// Resolve the backend's base URL from the host the page was actually opened on, so the API
// auto-follows whatever address reached us — LAN IP, Tailscale IP, or localhost — with no rebuild.
// (DHCP can rotate the LAN IP; deriving it at runtime means we never have to re-bake it.)
// Falls back to the build-time env var, then localhost, for any non-browser (SSR) context.
export function apiBase(): string {
    if (typeof window !== 'undefined' && window.location?.hostname) {
        return `${window.location.protocol}//${window.location.hostname}:8000`;
    }
    return process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
}
