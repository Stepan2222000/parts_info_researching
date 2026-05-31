// Server-only: base URL of the Python FastAPI process. The browser never calls
// it directly — Next route handlers proxy to it (same-origin for the client).
export const API_BASE = process.env.PARTS_RESEARCH_API_URL || "http://127.0.0.1:8000";
