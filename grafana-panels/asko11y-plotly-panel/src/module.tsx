import React, { useCallback, useEffect, useRef, useState } from "react";
import { PanelPlugin } from "@grafana/data";
import { useTheme2 } from "@grafana/ui";
import Plotly from "plotly.js-dist-min";
import { withRangebreakSafeTraces } from "./rangebreaks.mjs";
import { resolveRenderMode } from "./renderMode.mjs";
import { applyGrafanaTheme } from "./theme.mjs";

type PlotlyFigure = {
  data: Array<Record<string, unknown>>;
  layout: Record<string, unknown>;
  config?: Record<string, unknown>;
  error?: string;
};

type PlotlyRuntime = typeof Plotly & {
  Plots: { resize(element: HTMLElement): void };
};
const plotlyRuntime = Plotly as PlotlyRuntime;

type Options = {
  renderMode?: "image" | "plotly";
  figure?: PlotlyFigure;
  fallbackUrl?: string;
  alt?: string;
};

type Theme = ReturnType<typeof useTheme2>;

function PlotFigure({
  figure,
  alt,
  theme,
  height,
  onFailure,
}: {
  figure: PlotlyFigure;
  alt: string;
  theme: Theme;
  height: number;
  onFailure: () => void;
}) {
  const container = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!container.current) {
      return;
    }
    const element = container.current;
    let active = true;
    // Plotly mutates inputs; the original figure must remain unchanged.
    const rendered = structuredClone(figure);
    const data = withRangebreakSafeTraces(rendered.data, rendered.layout);
    const layout = applyGrafanaTheme(rendered.layout, theme);
    const config = {
      ...rendered.config,
      displaylogo: false,
      responsive: true,
      scrollZoom: false,
    };
    try {
      Promise.resolve(
        Plotly.react(element, data, layout, config),
      ).catch(() => {
        if (active) {
          onFailure();
        }
      });
    } catch {
      onFailure();
    }
    const observer = new ResizeObserver(() =>
      plotlyRuntime.Plots.resize(element),
    );
    observer.observe(element);
    return () => {
      active = false;
      observer.disconnect();
      Plotly.purge(element);
    };
  }, [onFailure, theme, figure]);

  return (
    <div
      ref={container}
      role="figure"
      aria-label={alt}
      style={{ width: "100%", height, minHeight: 320 }}
    />
  );
}

function PlotlyPanel({
  options,
  height,
}: {
  options: Options;
  height: number;
}) {
  const theme = useTheme2();
  const [failedFigure, setFailedFigure] = useState<PlotlyFigure | null>(null);
  const failed = !!options.figure && failedFigure === options.figure;
  const mode = failed ? "error" : resolveRenderMode(options);
  const onFailure = useCallback(
    () => setFailedFigure(options.figure ?? null),
    [options.figure],
  );
  const alt = options.alt || "分析圖表";

  if (mode === "image") {
    return (
      <figure style={{ margin: 0, height: "100%", overflow: "auto" }}>
        <img
          src={options.fallbackUrl}
          alt={alt}
          style={{ width: "100%", height: "100%", objectFit: "contain" }}
        />
      </figure>
    );
  }
  if (mode === "error" || !options.figure) {
    return (
      <div
        role="alert"
        style={{ padding: 16, height: "100%", overflow: "auto" }}
      >
        圖表無法呈現；未自動降級或修改資料，原始執行結果仍保留。
      </div>
    );
  }
  return (
    <div style={{ height: "100%", overflow: "auto" }}>
      <PlotFigure
        figure={options.figure}
        alt={alt}
        theme={theme}
        height={Math.max(320, height)}
        onFailure={onFailure}
      />
    </div>
  );
}

export const plugin = new PanelPlugin<Options>(PlotlyPanel).setPanelOptions(
  (builder) =>
    builder
      .addTextInput({ path: "alt", name: "替代文字" })
      .addTextInput({ path: "fallbackUrl", name: "Authorized image URL" }),
);
