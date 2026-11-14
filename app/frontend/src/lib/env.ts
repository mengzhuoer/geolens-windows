interface EnvConfig {
  API_BASE_URL: string;
  TILE_BASE_URL: string;
  /** "1" on the bundled Windows build: sign in as admin without a login screen. */
  AUTO_LOGIN: string;
}

declare global {
  interface Window {
    __ENV__?: Partial<EnvConfig>;
  }
}

const defaults: EnvConfig = {
  API_BASE_URL: '',
  TILE_BASE_URL: '',
  AUTO_LOGIN: '',
};

export function getEnvConfig(): EnvConfig {
  const env = window.__ENV__;
  if (!env) return { ...defaults };
  return {
    API_BASE_URL: env.API_BASE_URL ?? '',
    TILE_BASE_URL: env.TILE_BASE_URL ?? '',
    AUTO_LOGIN: env.AUTO_LOGIN ?? '',
  };
}
