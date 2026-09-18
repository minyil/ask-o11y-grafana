import { withSubpath } from '../utils/subpath';

let sessionRefresh: Promise<boolean> | undefined;

async function refreshGrafanaSession(): Promise<boolean> {
  if (!sessionRefresh) {
    sessionRefresh = fetch(withSubpath('/api/login/ping'), { credentials: 'same-origin' })
      .then((response) => response.ok)
      .catch(() => false)
      .finally(() => {
        sessionRefresh = undefined;
      });
  }
  return sessionRefresh;
}

/**
 * Grafana rotates browser session tokens while the user is active. Native
 * fetch calls from an app plugin do not use Grafana's auth-refresh wrapper, so
 * retry once after the safe login ping when the auth middleware returns 401.
 */
export async function grafanaFetch(input: string | URL, init: RequestInit = {}): Promise<Response> {
  const requestInit: RequestInit = {
    ...init,
    credentials: init.credentials ?? 'same-origin',
  };
  const response = await fetch(input, requestInit);
  if (response.status !== 401 || requestInit.signal?.aborted) {
    return response;
  }

  if (!(await refreshGrafanaSession()) || requestInit.signal?.aborted) {
    return response;
  }

  return fetch(input, requestInit);
}
