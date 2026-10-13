declare module "@grafana/data" {
  import type React from "react";

  export class PanelPlugin<TOptions> {
    constructor(
      component: React.ComponentType<{
        options: TOptions;
        width: number;
        height: number;
      }>,
    );
  }
}

declare module "@grafana/ui" {
  type Rich = { main: string; text: string };
  export function useTheme2(): {
    colors: {
      text: { primary: string; secondary: string };
      border: { weak: string; medium: string };
      background: { primary: string; secondary: string };
      primary: Rich & { contrastText: string };
      warning: Rich;
    };
    typography: { fontFamily: string };
  };
}
