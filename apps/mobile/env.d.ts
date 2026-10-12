/** Minimal env typings so EXPO_PUBLIC_ variables typecheck without @types/node. */
declare const process: {
  env: { [key: string]: string | undefined };
};
