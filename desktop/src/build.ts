declare const __SAM_BUILD__: {
  version: string;
  commit: string;
  modified: boolean;
  builtAt: string;
};

/** Which build of the app this is (set at build time; see vite.config.ts). */
export const BUILD_INFO = __SAM_BUILD__;
