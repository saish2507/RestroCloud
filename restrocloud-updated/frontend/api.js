// Browser-side API client. Keep the backend URL aligned with docker-compose.yml.
const API_URL = "http://localhost:5000/api";

function getToken() {
    return localStorage.getItem("token");
}

function getUser() {
    const raw = localStorage.getItem("user");
    if (!raw) return null;
    try {
        return JSON.parse(raw);
    } catch (_error) {
        clearLogin();
        return null;
    }
}

function saveLogin(token, user) {
    localStorage.setItem("token", token);
    localStorage.setItem("user", JSON.stringify(user));
}

function clearLogin() {
    localStorage.removeItem("token");
    localStorage.removeItem("user");
}

// Adds auth headers, serializes JSON, and turns API failures into readable errors.
async function apiRequest(path, method = "GET", body = null) {
    const headers = { "Content-Type": "application/json" };
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;

    const response = await fetch(API_URL + path, {
        method,
        headers,
        body: body === null ? null : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.error || `Request failed (${response.status}).`);
    }
    return data;
}
