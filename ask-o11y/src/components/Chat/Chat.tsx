import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { Icon, useTheme2 } from '@grafana/ui';

import { useChat } from './hooks/useChat';
import { useKeyboardNavigation } from './hooks/useKeyboardNavigation';
import { useEmbeddingAllowed } from './hooks/useEmbeddingAllowed';
import { useChatScene } from './hooks/useChatScene';
import { useSidePanelState } from './hooks/useSidePanelState';
import { ChatInterfaceState } from './scenes/ChatInterfaceScene';
import { GrafanaPageState } from './scenes/GrafanaPageScene';
import {
  SessionSidebar,
  NewChatButton,
  UploadButton,
  HistoryButton,
  SaveToMemoryButton,
  ModelSelector,
  McpConnectionsButton,
} from './components';
import { ChatInputRef } from './components/ChatInput/ChatInput';
import { ChatErrorBoundary } from '../ErrorBoundary';
import type { SessionMetadata } from './hooks/useSessionManager';
import type { ChatMessage } from './types';
import type { AppPluginSettings } from '../../types/plugin';
import {
  MAX_UPLOADS_PER_SESSION,
  removeUploadedDataset,
  uploadedDatasetsMessage,
  type UploadedDataset,
} from '../../services/uploadClient';
import { testIds } from '../testIds';
import {
  formatModelLabel,
  formatModelSelectionLabel,
  listLLMModelOptions,
  type LLMModelOption,
  type LLMModelSelection,
} from '../../services/llmModels';
import { listSkills, toSkillCommands, type SkillCommand } from '../../services/skillsClient';

interface ChatProps {
  pluginSettings: AppPluginSettings;
  readOnly?: boolean;
  initialSession?: { id?: string; messages?: ChatMessage[] };
  initialMessage?: string;
  initialMessageType?: 'chat' | 'investigation' | 'performance';
  initialSkill?: string;
  sessionIdFromUrl: string | null;
  onSessionIdChange: (sessionId: string | null) => void;
}

function ChatComponent({
  pluginSettings,
  readOnly = false,
  initialSession,
  initialMessage,
  initialMessageType,
  initialSkill,
  sessionIdFromUrl,
  onSessionIdChange,
}: ChatProps): React.ReactElement | null {
  const theme = useTheme2();
  const allowEmbedding = useEmbeddingAllowed();
  const [modelOptions, setModelOptions] = useState<LLMModelOption[]>([]);
  const [selectedModel, setSelectedModel] = useState<LLMModelSelection>('auto');
  const [skillCommands, setSkillCommands] = useState<SkillCommand[]>([]);
  const [uploaded, setUploaded] = useState<{ datasets: UploadedDataset[]; sessionId: string } | null>(null);

  const kioskModeEnabled = pluginSettings?.kioskModeEnabled ?? true;
  const chatPanelPosition = pluginSettings?.chatPanelPosition || 'right';

  useEffect(() => {
    let cancelled = false;
    listLLMModelOptions()
      .then((options) => {
        if (cancelled) {
          return;
        }
        setModelOptions(options);
        setSelectedModel((prev) => (options.some((option) => option.value === prev) ? prev : 'auto'));
      })
      .catch(() => {
        if (!cancelled) {
          setModelOptions([]);
        }
      });
    listSkills()
      .then((skills) => {
        if (!cancelled) {
          setSkillCommands(toSkillCommands(skills));
        }
      })
      .catch(() => {
        // Skills are optional; the slash menu simply stays hidden.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const skillNames = useMemo(() => skillCommands.map((command) => command.name), [skillCommands]);

  const {
    chatHistory,
    currentInput,
    isGenerating,
    chatContainerRef,
    setCurrentInput,
    sendMessage,
    retryLastMessage,
    handleKeyPress,
    clearChat,
    sessionManager,
    bottomSpacerRef,
    detectedPageRefs,
    messageQueue,
    stopGeneration,
    resolveApproval,
  } = useChat(
    pluginSettings,
    sessionIdFromUrl,
    onSessionIdChange,
    readOnly ? initialSession : undefined,
    readOnly,
    initialMessage,
    initialMessageType,
    selectedModel,
    initialSkill,
    skillNames
  );

  const chatInputRef = useRef<ChatInputRef>(null);
  // Docked history panel is shown by default in the interactive chat so past
  // conversations are discoverable; hidden in read-only/shared views.
  const [isHistoryOpen, setIsHistoryOpen] = useState(!readOnly);

  const {
    visiblePageRefs,
    showSidePanel,
    handleRemoveTab,
    handleClose: handleSidePanelClose,
    handleToggle: handleSidePanelToggle,
  } = useSidePanelState({
    detectedPageRefs,
    currentSessionId: sessionManager.currentSessionId,
    allowEmbedding,
  });

  const containerRef = useRef<HTMLDivElement>(null);

  const openHistory = useCallback(() => {
    setIsHistoryOpen(true);
  }, []);

  useKeyboardNavigation(containerRef);

  const handleSuggestionClick = useCallback(
    (message: string, skill?: string) => {
      setCurrentInput(skill ? `/${skill} ${message}` : message);
      setTimeout(() => {
        chatInputRef.current?.focus();
      }, 100);
    },
    [setCurrentInput]
  );

  const currentUploads = useMemo(
    () => (uploaded && uploaded.sessionId === sessionManager.currentSessionId ? uploaded.datasets : []),
    [uploaded, sessionManager.currentSessionId]
  );

  const handleUploaded = useCallback(
    (sessionId: string, datasets: UploadedDataset[]) => {
      const joined = uploaded?.sessionId === sessionId ? [...uploaded.datasets, ...datasets] : datasets;
      setUploaded({ datasets: joined, sessionId });
      const showMessage = () => {
        setCurrentInput(uploadedDatasetsMessage(joined));
        setTimeout(() => chatInputRef.current?.focus(), 100);
      };
      if (sessionId === sessionManager.currentSessionId) {
        showMessage();
      } else {
        void sessionManager.loadSession(sessionId).then(showMessage);
      }
    },
    [sessionManager, setCurrentInput, uploaded]
  );

  const handleRemoveUpload = useCallback((dataset: UploadedDataset, sessionId: string) => {
    void removeUploadedDataset(dataset.dataset_id, sessionId)
      .then(() =>
        setUploaded((previous) =>
          previous && previous.sessionId === sessionId
            ? { sessionId, datasets: previous.datasets.filter((item) => item.dataset_id !== dataset.dataset_id) }
            : previous
        )
      )
      .catch((error) => window.alert(error instanceof Error ? error.message : 'Remove failed'));
  }, []);

  const currentSession = sessionManager.sessions.find((s: SessionMetadata) => s.id === sessionManager.currentSessionId);
  const currentSessionTitle = currentSession?.title;
  const sessionModel = currentSession?.model;
  const selectedModelOption = modelOptions.find((option) => option.value === selectedModel);
  const sessionModelOption = modelOptions.find((option) => option.value === sessionModel);
  const currentModelLabel = sessionModel
    ? sessionModelOption?.label || formatModelLabel(sessionModel)
    : chatHistory.length > 0
    ? selectedModelOption?.label || formatModelSelectionLabel(selectedModel)
    : undefined;
  const hasMessages = chatHistory.length > 0;
  const graphitiEnabled = pluginSettings.mcpServers?.some((s) => s.id === 'graphiti' && s.enabled) ?? false;
  const showModelSelector = !readOnly && modelOptions.length > 0 && !sessionModel;
  const modelSelector = useMemo(
    () =>
      showModelSelector ? (
        <ModelSelector
          options={modelOptions}
          value={selectedModel}
          disabled={isGenerating}
          onChange={setSelectedModel}
        />
      ) : undefined,
    [showModelSelector, modelOptions, selectedModel, isGenerating]
  );

  const chatInterfaceState: ChatInterfaceState = useMemo(
    () => ({
      chatHistory,
      currentInput,
      isGenerating,
      currentSessionTitle,
      currentModelLabel,
      setCurrentInput,
      sendMessage,
      handleKeyPress,
      chatContainerRef,
      chatInputRef,
      bottomSpacerRef,
      leftSlot: (
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <NewChatButton onConfirm={clearChat} isGenerating={isGenerating} />
            <UploadButton
              disabled={isGenerating}
              targetSessionId={currentUploads.length > 0 ? uploaded?.sessionId : undefined}
              remaining={MAX_UPLOADS_PER_SESSION - currentUploads.length}
              onUploaded={handleUploaded}
            />
            {modelSelector}
          </div>
          {uploaded &&
            currentUploads.map((dataset) => (
              <div
                key={dataset.dataset_id}
                className="flex items-center gap-2 text-xs text-secondary"
                data-testid={testIds.chat.uploadedDataset(dataset.dataset_id)}
              >
                <span className="truncate" title={dataset.filename}>
                  {dataset.filename} · {dataset.rows} rows · {dataset.columns} columns
                  {dataset.sheet ? ` · ${dataset.sheet}` : ''}
                </span>
                <button
                  type="button"
                  className="text-error"
                  onClick={() => handleRemoveUpload(dataset, uploaded.sessionId)}
                >
                  Remove
                </button>
              </div>
            ))}
        </div>
      ),
      rightSlot: (
        <div className="flex items-center gap-1">
          {graphitiEnabled && hasMessages && <SaveToMemoryButton messages={chatHistory} />}
          <McpConnectionsButton />
          <HistoryButton onClick={openHistory} sessionCount={sessionManager.sessions.length} />
        </div>
      ),
      readOnly,
      skillCommands,
      onSuggestionClick: handleSuggestionClick,
      queuedMessageCount: messageQueue.length,
      onStopGeneration: stopGeneration,
      onResolveApproval: resolveApproval,
      onRetry: retryLastMessage,
    }),
    [
      chatHistory,
      currentInput,
      isGenerating,
      currentSessionTitle,
      currentModelLabel,
      sessionManager.sessions.length,
      uploaded,
      currentUploads,
      handleRemoveUpload,
      setCurrentInput,
      sendMessage,
      handleKeyPress,
      chatContainerRef,
      chatInputRef,
      bottomSpacerRef,
      hasMessages,
      modelSelector,
      skillCommands,
      graphitiEnabled,
      clearChat,
      handleUploaded,
      openHistory,
      readOnly,
      handleSuggestionClick,
      messageQueue.length,
      stopGeneration,
      resolveApproval,
      retryLastMessage,
    ]
  );

  const grafanaPageState: GrafanaPageState = useMemo(
    () => ({
      pageRefs: visiblePageRefs,
      activeTabIndex: 0,
      kioskModeEnabled,
      onRemoveTab: handleRemoveTab,
      onClose: handleSidePanelClose,
    }),
    [visiblePageRefs, handleRemoveTab, kioskModeEnabled, handleSidePanelClose]
  );

  const chatScene = useChatScene(showSidePanel, chatInterfaceState, grafanaPageState, chatPanelPosition);

  return (
    <div
      ref={containerRef}
      className="w-full h-full flex relative"
      role="main"
      aria-label="Chat interface"
      style={{
        backgroundColor: theme.colors.background.canvas,
      }}
    >
      <SessionSidebar
        sessionManager={sessionManager}
        currentSessionId={sessionManager.currentSessionId}
        isOpen={isHistoryOpen}
        onClose={() => setIsHistoryOpen(false)}
        docked={!readOnly}
      />

      {chatScene && (
        <div data-plugin-split-layout style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
          <chatScene.Component model={chatScene} />
        </div>
      )}
      {visiblePageRefs.length > 0 && allowEmbedding === true && !showSidePanel && (
        <button
          onClick={handleSidePanelToggle}
          className="absolute right-0 top-4 z-10 flex items-center gap-1 rounded-l-md border px-2 py-2 text-xs font-medium shadow"
          aria-label="Show dashboard preview"
          title="Show preview"
          style={{
            backgroundColor: theme.colors.background.primary,
            borderColor: theme.colors.border.weak,
            color: theme.colors.text.primary,
          }}
        >
          <Icon name="columns" size="sm" />
          <span>Preview</span>
        </button>
      )}
    </div>
  );
}

export function Chat(props: ChatProps): React.ReactElement {
  return (
    <ChatErrorBoundary>
      <ChatComponent {...props} />
    </ChatErrorBoundary>
  );
}
