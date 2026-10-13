const path = require("path");

module.exports = {
  mode: "production",
  entry: "./src/module.tsx",
  devtool: "source-map",
  output: {
    filename: "module.js",
    path: path.resolve(__dirname, "dist"),
    libraryTarget: "amd",
    globalObject: "globalThis",
    clean: true,
  },
  externals: {
    react: "react",
    "@grafana/data": "@grafana/data",
    "@grafana/ui": "@grafana/ui",
  },
  module: {
    rules: [
      {
        test: /\.tsx?$/,
        exclude: /node_modules/,
        use: {
          loader: "swc-loader",
          options: {
            jsc: {
              parser: { syntax: "typescript", tsx: true },
              transform: { react: { runtime: "classic" } },
            },
          },
        },
      },
    ],
  },
  resolve: { extensions: [".tsx", ".ts", ".mjs", ".js"] },
  optimization: { minimize: true },
};
