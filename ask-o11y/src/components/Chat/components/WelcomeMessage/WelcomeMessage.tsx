import React from 'react';
import { useTheme2 } from '@grafana/ui';
import { SparkleIcon } from '../../../icons';

export const WelcomeMessage: React.FC = () => {
  const theme = useTheme2();

  return (
    <div className="text-center animate-fadeIn flex flex-col items-center justify-center py-8">
      {/* Greeting */}
      <p className="text-base mb-2" style={{ color: theme.colors.text.secondary }}>
        嗨，我是
      </p>

      {/* Title with sparkle icon */}
      <div className="flex items-center justify-center gap-3 mb-5">
        <SparkleIcon size={44} color={theme.colors.text.primary} className="animate-sparkle" />
        <h1 className="text-5xl font-bold tracking-tight" style={{ color: theme.colors.text.primary }}>
          數據分析助手
        </h1>
      </div>

      {/* Description with highlighted text */}
      <p className="text-lg max-w-2xl mx-auto leading-relaxed" style={{ color: theme.colors.text.secondary }}>
        <span className="font-medium" style={{ color: theme.colors.warning.main }}>
          透過自然語言
        </span>
        ，協助你查詢數據、分析趨勢、調查異常與管理儀表板，讓數據分析更直覺，快速掌握關鍵資訊。
      </p>
    </div>
  );
};
