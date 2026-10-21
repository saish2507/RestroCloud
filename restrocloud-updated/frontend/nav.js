// Fills in the right side of the navbar depending on whether someone is
// logged in, and wires up the logout link. Every page includes this.
function escapeNavText(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
}

function renderNav() {
    const user = getUser();
    const authLinks = document.getElementById("auth-links");

    if (!user) {
        authLinks.innerHTML = `
            <a href="login.html">Login</a>
            <a href="register.html">Register</a>
        `;
        return;
    }

    const roleLink = user.role === "owner"
        ? `<a href="dashboard.html">My Dashboard</a>`
        : `<a href="orders.html">My Orders</a>`;

    authLinks.innerHTML = `
        ${roleLink}
        <span class="greeting">Hi, ${escapeNavText(user.name)}</span>
        <a href="#" id="logout-link">Logout</a>
    `;

    document.getElementById("logout-link").addEventListener("click", async (event) => {
        event.preventDefault();
        await apiRequest("/logout", "POST").catch(() => {});
        clearLogin();
        window.location.href = "index.html";
    });
}

document.addEventListener("DOMContentLoaded", renderNav);
