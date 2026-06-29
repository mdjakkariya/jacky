import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/daemon.js", () => {
  // Captured WS subscriptions so tests can emit mcp_oauth / mcp_status events.
  const handlers = {};
  return {
    daemon: {
      addMcpServer: vi.fn().mockResolvedValue({ ok: true, server: "slack" }),
      mcpSetToken: vi.fn().mockResolvedValue({ ok: true }),
      enableMcpServer: vi.fn().mockResolvedValue({ ok: true }),
      mcpAuthStart: vi.fn().mockResolvedValue({ ok: true, started: true }),
      secret: vi.fn().mockResolvedValue({ ok: true }),
      on: vi.fn((type, fn) => {
        (handlers[type] = handlers[type] || []).push(fn);
        return () => { handlers[type] = (handlers[type] || []).filter((h) => h !== fn); };
      }),
      __emit: (type, msg) => { (handlers[type] || []).forEach((fn) => fn(msg)); },
      __reset: () => { for (const k in handlers) delete handlers[k]; },
    },
  };
});
import { daemon } from "../../lib/daemon.js";
import { showAddConnection, hideAddConnection } from "./add-connection.js";

function makeContainer() {
  const el = document.createElement("div");
  document.body.appendChild(el);
  return el;
}

/** Ensure the step-2 URL field is non-empty so HTTP navigation isn't blocked by the
 *  "URL required" gate. Catalog entries now ship official URLs, so this only fills a
 *  truly empty field (custom servers); no-op for stdio entries (no URL field). */
function fillUrl(container, url = "https://slack.test/mcp") {
  const u = container.querySelector("[data-field='url']");
  if (u && !u.value) u.value = url;
}

beforeEach(() => {
  vi.clearAllMocks();
  daemon.__reset();
  document.body.innerHTML = "";
});

// ---------------------------------------------------------------------------
// Step 1 — Source (catalog)
// ---------------------------------------------------------------------------
describe("Step 1 — catalog", () => {
  it("renders catalog items including Custom as the last item", () => {
    const container = makeContainer();
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    const items = container.querySelectorAll(".cat-item");
    // 4 catalog entries + 1 custom = 5
    expect(items.length).toBe(5);
    const labels = [...items].map((el) => el.querySelector(".cat-name").textContent);
    expect(labels[labels.length - 1]).toBe("Custom MCP server");
  });

  it("clicking a catalog item selects it (adds 'selected' class)", () => {
    const container = makeContainer();
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    const items = container.querySelectorAll(".cat-item");
    items[1].click(); // GitHub
    expect(items[1].classList.contains("selected")).toBe(true);
    // others not selected
    expect(items[0].classList.contains("selected")).toBe(false);
  });

  it("Cancel button calls onCancel", () => {
    const container = makeContainer();
    const onCancel = vi.fn();
    showAddConnection(container, { onDone: vi.fn(), onCancel });
    const cancelBtn = container.querySelector(".btn-cancel");
    cancelBtn.click();
    expect(onCancel).toHaveBeenCalled();
  });

  it("progress bar shows 'now' on step 1 and blank on remaining", () => {
    const container = makeContainer();
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    const steps = container.querySelectorAll(".wizard-step");
    expect(steps[0].classList.contains("now")).toBe(true);
    expect(steps[1].classList.contains("now")).toBe(false);
    expect(steps[1].classList.contains("done")).toBe(false);
    expect(steps[2].classList.contains("now")).toBe(false);
    expect(steps[3].classList.contains("now")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Step 1 → Step 2 navigation
// ---------------------------------------------------------------------------
describe("Step 1 → Step 2 navigation", () => {
  it("Continue from step 1 advances to step 2", () => {
    const container = makeContainer();
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    // Select Slack (first item) and continue
    container.querySelector(".cat-item").click();
    container.querySelector(".btn-next").click();
    // Step 2 should be visible
    expect(container.querySelector("[data-step='2']")).not.toBeNull();
    expect(container.querySelector("[data-step='1']")).toBeNull();
  });

  it("progress bar shows step 1 done and step 2 now after continuing", () => {
    const container = makeContainer();
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    container.querySelector(".cat-item").click();
    container.querySelector(".btn-next").click();
    const steps = container.querySelectorAll(".wizard-step");
    expect(steps[0].classList.contains("done")).toBe(true);
    expect(steps[1].classList.contains("now")).toBe(true);
  });

  it("Back from step 2 returns to step 1", () => {
    const container = makeContainer();
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    container.querySelector(".cat-item").click();
    container.querySelector(".btn-next").click();
    // Now on step 2 — click back
    container.querySelector(".btn-back").click();
    expect(container.querySelector("[data-step='1']")).not.toBeNull();
    expect(container.querySelector("[data-step='2']")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Step 2 — Transport
// ---------------------------------------------------------------------------
describe("Step 2 — Transport", () => {
  function goToStep2(container, catalogIndex = 0) {
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    const items = container.querySelectorAll(".cat-item");
    items[catalogIndex].click();
    container.querySelector(".btn-next").click();
  }

  it("catalog pick (Slack, HTTP) pre-fills its official URL (editable)", () => {
    const container = makeContainer();
    goToStep2(container, 0); // Slack
    const urlField = container.querySelector("[data-field='url']");
    expect(urlField).not.toBeNull();
    expect(urlField.value).toBe("https://mcp.slack.com/mcp");
    expect(urlField.readOnly).toBe(false);
  });

  it("catalog pick (GitHub, HTTP) pre-fills its official URL", () => {
    const container = makeContainer();
    goToStep2(container, 1); // GitHub
    const urlField = container.querySelector("[data-field='url']");
    expect(urlField.value).toBe("https://api.githubcopilot.com/mcp/");
  });

  it("catalog pick (Local Files, stdio) pre-fills stdio and shows command field", () => {
    const container = makeContainer();
    goToStep2(container, 2); // Local Files (index 2)
    const cmdField = container.querySelector("[data-field='command']");
    expect(cmdField).not.toBeNull();
    expect(cmdField.value).toBe("npx @mcp/server-files");
  });

  it("Custom MCP: selecting HTTP transport radio shows URL field and hides command fields", () => {
    const container = makeContainer();
    // Select "Custom" (last item, index 4)
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    const items = container.querySelectorAll(".cat-item");
    items[4].click(); // Custom
    container.querySelector(".btn-next").click();
    // Click HTTP radio
    const httpRadio = container.querySelector("[data-transport='http']");
    httpRadio.click();
    expect(container.querySelector("[data-field='url']")).not.toBeNull();
    const cmdField = container.querySelector("[data-field='command']");
    // command field should be hidden/absent
    expect(!cmdField || cmdField.closest(".hidden") || cmdField.style.display === "none" || cmdField.classList.contains("hidden")).toBe(true);
  });

  it("Custom MCP: selecting stdio transport radio shows command+args and hides URL", () => {
    const container = makeContainer();
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    const items = container.querySelectorAll(".cat-item");
    items[4].click(); // Custom
    container.querySelector(".btn-next").click();
    // Click stdio radio
    const stdioRadio = container.querySelector("[data-transport='stdio']");
    stdioRadio.click();
    const cmdField = container.querySelector("[data-field='command']");
    expect(cmdField).not.toBeNull();
    expect(!cmdField.closest(".hidden") && cmdField.style.display !== "none" && !cmdField.classList.contains("hidden")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Step 2 → Step 3 navigation
// ---------------------------------------------------------------------------
describe("Step 2 → Step 3 navigation", () => {
  function goToStep3(container) {
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    // Select Slack
    container.querySelector(".cat-item").click();
    container.querySelector(".btn-next").click();
    fillUrl(container); // Slack ships blank — provide a URL to pass the gate
    // Continue to step 3
    container.querySelector(".btn-next").click();
  }

  it("Continue from step 2 advances to step 3", () => {
    const container = makeContainer();
    goToStep3(container);
    expect(container.querySelector("[data-step='3']")).not.toBeNull();
    expect(container.querySelector("[data-step='2']")).toBeNull();
  });

  it("progress bar on step 3: steps 1+2 done, step 3 now", () => {
    const container = makeContainer();
    goToStep3(container);
    const steps = container.querySelectorAll(".wizard-step");
    expect(steps[0].classList.contains("done")).toBe(true);
    expect(steps[1].classList.contains("done")).toBe(true);
    expect(steps[2].classList.contains("now")).toBe(true);
    expect(steps[3].classList.contains("now")).toBe(false);
    expect(steps[3].classList.contains("done")).toBe(false);
  });

  it("Back from step 3 returns to step 2", () => {
    const container = makeContainer();
    goToStep3(container);
    container.querySelector(".btn-back").click();
    expect(container.querySelector("[data-step='2']")).not.toBeNull();
    expect(container.querySelector("[data-step='3']")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Step 3 — Auth
// ---------------------------------------------------------------------------
describe("Step 3 — Auth", () => {
  function goToStep3(container, onDone = vi.fn(), onCancel = vi.fn()) {
    showAddConnection(container, { onDone, onCancel });
    container.querySelector(".cat-item").click();
    container.querySelector(".btn-next").click();
    fillUrl(container);
    container.querySelector(".btn-next").click();
  }

  it("renders OAuth and Token options", () => {
    const container = makeContainer();
    goToStep3(container);
    const opts = container.querySelectorAll(".opt-item");
    const texts = [...opts].map((o) => o.querySelector(".opt-title").textContent);
    expect(texts.some((t) => t.toLowerCase().includes("oauth"))).toBe(true);
    expect(texts.some((t) => t.toLowerCase().includes("token"))).toBe(true);
  });

  it("Keychain banner is shown", () => {
    const container = makeContainer();
    goToStep3(container);
    const banner = container.querySelector(".keychain-banner");
    expect(banner).not.toBeNull();
  });

  it("Selecting Token and continuing advances to step 4 with token input", () => {
    const container = makeContainer();
    goToStep3(container);
    // Click Token option
    const opts = container.querySelectorAll(".opt-item");
    const tokenOpt = [...opts].find((o) => o.querySelector(".opt-title").textContent.toLowerCase().includes("token"));
    tokenOpt.click();
    container.querySelector(".btn-next").click();
    // Step 4 should show token input
    expect(container.querySelector("[data-step='4']")).not.toBeNull();
    const tokenInput = container.querySelector("[data-field='token']");
    expect(tokenInput).not.toBeNull();
  });

  it("Selecting OAuth and continuing advances to step 4 with hand-off explainer", () => {
    const container = makeContainer();
    goToStep3(container);
    // OAuth is the default (first option); click continue
    const opts = container.querySelectorAll(".opt-item");
    const oauthOpt = [...opts].find((o) => o.querySelector(".opt-title").textContent.toLowerCase().includes("oauth"));
    oauthOpt.click();
    container.querySelector(".btn-next").click();
    // Step 4 should show OAuth explainer
    expect(container.querySelector("[data-step='4']")).not.toBeNull();
    expect(container.querySelector(".oauth-explainer")).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Step 4 — Token path: daemon calls
// ---------------------------------------------------------------------------
describe("Step 4 — Token path", () => {
  async function goToTokenStep4(container, onDone = vi.fn()) {
    showAddConnection(container, { onDone, onCancel: vi.fn() });
    // Select Slack
    container.querySelector(".cat-item").click();
    container.querySelector(".btn-next").click();
    // Step 2 transport — provide a URL, then continue
    fillUrl(container);
    container.querySelector(".btn-next").click();
    // Step 3 auth — select token
    const opts = container.querySelectorAll(".opt-item");
    const tokenOpt = [...opts].find((o) => o.querySelector(".opt-title").textContent.toLowerCase().includes("token"));
    tokenOpt.click();
    container.querySelector(".btn-next").click();
  }

  it("token step 4 shows token input field", async () => {
    const container = makeContainer();
    await goToTokenStep4(container);
    expect(container.querySelector("[data-field='token']")).not.toBeNull();
  });

  it("submitting token calls addMcpServer, mcpSetToken, enableMcpServer in order then onDone", async () => {
    const container = makeContainer();
    const onDone = vi.fn();
    await goToTokenStep4(container, onDone);

    // Enter a token
    const tokenInput = container.querySelector("[data-field='token']");
    tokenInput.value = "xoxb-test-token";

    // Click connect
    const connectBtn = container.querySelector(".btn-connect");
    connectBtn.click();

    // Wait for async operations
    await new Promise((r) => setTimeout(r, 0));

    expect(daemon.addMcpServer).toHaveBeenCalledOnce();
    const descriptor = daemon.addMcpServer.mock.calls[0][0];
    expect(descriptor.id).toBe("slack");
    expect(descriptor.transport).toBe("http");
    expect(descriptor.auth).toEqual({ type: "token" });
    expect(descriptor.egress).toBe("network");

    expect(daemon.mcpSetToken).toHaveBeenCalledWith("slack", "xoxb-test-token");
    expect(daemon.enableMcpServer).toHaveBeenCalledWith("slack");
    expect(onDone).toHaveBeenCalled();

    // Verify call order using mock.invocationCallOrder
    const addOrder = daemon.addMcpServer.mock.invocationCallOrder[0];
    const tokenOrder = daemon.mcpSetToken.mock.invocationCallOrder[0];
    const enableOrder = daemon.enableMcpServer.mock.invocationCallOrder[0];
    expect(addOrder).toBeLessThan(tokenOrder);
    expect(tokenOrder).toBeLessThan(enableOrder);
  });
});

// ---------------------------------------------------------------------------
// Step 4 — OAuth path: registers + enables the server, then shows live progress
// ---------------------------------------------------------------------------
describe("Step 4 — OAuth path", () => {
  async function goToOAuthStep4(container, onDone = vi.fn()) {
    showAddConnection(container, { onDone, onCancel: vi.fn() });
    container.querySelector(".cat-item").click(); // Slack
    container.querySelector(".btn-next").click();
    fillUrl(container);
    container.querySelector(".btn-next").click();
    // OAuth is first option — click to ensure selected
    const opts = container.querySelectorAll(".opt-item");
    const oauthOpt = [...opts].find((o) => o.querySelector(".opt-title").textContent.toLowerCase().includes("oauth"));
    oauthOpt.click();
    container.querySelector(".btn-next").click();
  }

  it("OAuth step 4 shows the hand-off explainer", async () => {
    const container = makeContainer();
    await goToOAuthStep4(container);
    expect(container.querySelector(".oauth-explainer")).not.toBeNull();
  });

  it("GitHub (built-in client_id) shows the built-in app note and NO client_id input on step 4", () => {
    const container = makeContainer();
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    container.querySelectorAll(".cat-item")[1].click(); // GitHub (auth: oauth, baked-in client_id)
    container.querySelector(".btn-next").click(); // step 2 (URL prefilled)
    container.querySelector(".btn-next").click(); // step 3 (oauth preselected)
    container.querySelector(".btn-next").click(); // step 4 (OAuth explainer)
    // Built-in app: credential inputs must NOT be rendered
    expect(container.querySelector("[data-field='client_id']")).toBeNull();
    expect(container.querySelector("[data-field='client_secret']")).toBeNull();
    // A note about the built-in app must be shown
    const note = container.querySelector(".oauth-creds-note");
    expect(note).not.toBeNull();
    expect(note.textContent.toLowerCase()).toContain("built-in");
  });

  it("GitHub (built-in client_id): 'Open browser' sends descriptor with client_id and does NOT call daemon.secret", async () => {
    const container = makeContainer();
    const onDone = vi.fn();
    showAddConnection(container, { onDone, onCancel: vi.fn() });
    container.querySelectorAll(".cat-item")[1].click(); // GitHub
    container.querySelector(".btn-next").click(); // step 2
    container.querySelector(".btn-next").click(); // step 3
    container.querySelector(".btn-next").click(); // step 4

    container.querySelector(".btn-open-browser").click();
    await new Promise((r) => setTimeout(r, 0));

    // descriptor must carry the baked-in client_id
    const descriptor = daemon.addMcpServer.mock.calls[0][0];
    expect(descriptor.client_id).toBe("Ov23livdLJSZe2WjUMrp");
    // No client_secret written to Keychain from the UI (comes from build-embedded file)
    expect(daemon.secret).not.toHaveBeenCalled();
    expect(daemon.enableMcpServer).toHaveBeenCalledWith("github");
  });

  it("'Open browser' registers + enables the server and shows progress (not coming-soon)", async () => {
    const container = makeContainer();
    await goToOAuthStep4(container);

    container.querySelector(".btn-open-browser").click();
    await new Promise((r) => setTimeout(r, 0));

    // Registers then enables (enabling connects → triggers the OAuth hand-off).
    expect(daemon.addMcpServer).toHaveBeenCalledOnce();
    const descriptor = daemon.addMcpServer.mock.calls[0][0];
    expect(descriptor.id).toBe("slack");
    expect(descriptor.auth).toEqual({ type: "oauth" });
    expect(daemon.enableMcpServer).toHaveBeenCalledWith("slack");
    // A live status line is shown — and it is NOT the old coming-soon stub.
    expect(container.querySelector(".oauth-coming-soon")).toBeNull();
    expect(container.querySelector(".oauth-status")).not.toBeNull();
  });

  it("reflects mcp_oauth stage events as progress text", async () => {
    const container = makeContainer();
    await goToOAuthStep4(container);
    container.querySelector(".btn-open-browser").click();
    await new Promise((r) => setTimeout(r, 0));

    daemon.__emit("mcp_oauth", { server: "slack", stage: "waiting_callback" });
    const status = container.querySelector(".oauth-status");
    expect(status.textContent.toLowerCase()).toContain("waiting");
  });

  it("calls onDone when mcp_status reports connected", async () => {
    vi.useFakeTimers();
    const container = makeContainer();
    const onDone = vi.fn();
    await goToOAuthStep4(container, onDone);
    container.querySelector(".btn-open-browser").click();
    await Promise.resolve(); // let the addMcpServer/enable promise chain settle
    await Promise.resolve();

    daemon.__emit("mcp_status", { server: "slack", state: "connected" });
    expect(container.querySelector(".oauth-status").textContent.toLowerCase()).toContain("connected");
    vi.advanceTimersByTime(700); // the onDone is fired after a short success delay
    expect(onDone).toHaveBeenCalled();
    vi.useRealTimers();
  });

  it("shows an error and does NOT call onDone when mcp_status reports error", async () => {
    const container = makeContainer();
    const onDone = vi.fn();
    await goToOAuthStep4(container, onDone);
    container.querySelector(".btn-open-browser").click();
    await new Promise((r) => setTimeout(r, 0));

    daemon.__emit("mcp_status", { server: "slack", state: "error", error: "bad url" });
    const status = container.querySelector(".oauth-status.error");
    expect(status).not.toBeNull();
    expect(onDone).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Step 4 — OAuth path with pre-registered client credentials
// ---------------------------------------------------------------------------
describe("Step 4 — OAuth path with pre-registered credentials", () => {
  async function goToOAuthStep4(container, onDone = vi.fn()) {
    showAddConnection(container, { onDone, onCancel: vi.fn() });
    container.querySelector(".cat-item").click(); // Slack
    container.querySelector(".btn-next").click();
    fillUrl(container);
    container.querySelector(".btn-next").click();
    // OAuth is first option — click to ensure selected
    const opts = container.querySelectorAll(".opt-item");
    const oauthOpt = [...opts].find((o) => o.querySelector(".opt-title").textContent.toLowerCase().includes("oauth"));
    oauthOpt.click();
    container.querySelector(".btn-next").click();
  }

  it("renders client_id and client_secret fields on OAuth step 4", async () => {
    const container = makeContainer();
    await goToOAuthStep4(container);
    expect(container.querySelector("[data-field='client_id']")).not.toBeNull();
    expect(container.querySelector("[data-field='client_secret']")).not.toBeNull();
  });

  it("shows the fixed redirect URI to register", async () => {
    const container = makeContainer();
    await goToOAuthStep4(container);
    const uriEl = container.querySelector(".oauth-redirect-uri");
    expect(uriEl).not.toBeNull();
    expect(uriEl.textContent).toBe("http://127.0.0.1:8975/callback");
  });

  it("'Open browser' with client_id sends descriptor with client_id and calls daemon.secret", async () => {
    const container = makeContainer();
    await goToOAuthStep4(container);

    // Fill in pre-registered credentials
    const clientIdInput = container.querySelector("[data-field='client_id']");
    const clientSecretInput = container.querySelector("[data-field='client_secret']");
    clientIdInput.value = "my-slack-client-id";
    clientSecretInput.value = "my-slack-secret";

    container.querySelector(".btn-open-browser").click();
    await new Promise((r) => setTimeout(r, 0));

    // descriptor must carry client_id
    const descriptor = daemon.addMcpServer.mock.calls[0][0];
    expect(descriptor.client_id).toBe("my-slack-client-id");

    // daemon.secret must be called with the correct Keychain account name and secret
    expect(daemon.secret).toHaveBeenCalledWith("mcp.slack.client_secret", "my-slack-secret");

    // enableMcpServer still called
    expect(daemon.enableMcpServer).toHaveBeenCalledWith("slack");
  });

  it("'Open browser' without client_id does NOT call daemon.secret", async () => {
    const container = makeContainer();
    await goToOAuthStep4(container);

    // Leave credentials blank
    container.querySelector(".btn-open-browser").click();
    await new Promise((r) => setTimeout(r, 0));

    expect(daemon.secret).not.toHaveBeenCalled();
    // descriptor must not carry client_id
    const descriptor = daemon.addMcpServer.mock.calls[0][0];
    expect(descriptor.client_id).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// hideAddConnection
// ---------------------------------------------------------------------------
describe("hideAddConnection", () => {
  it("removes the wizard card from the container", () => {
    const container = makeContainer();
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    expect(container.querySelector(".wizard-card")).not.toBeNull();
    hideAddConnection(container);
    expect(container.querySelector(".wizard-card")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Descriptor contract
// ---------------------------------------------------------------------------
describe("Descriptor contract", () => {
  async function buildDescriptorViaConnect(container, catalogIndex, extraSetup) {
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    const items = container.querySelectorAll(".cat-item");
    items[catalogIndex].click();
    container.querySelector(".btn-next").click(); // step 2
    if (extraSetup) extraSetup(container);
    fillUrl(container); // no-op for stdio entries (no URL field)
    container.querySelector(".btn-next").click(); // step 3
    // Select token auth
    const opts = container.querySelectorAll(".opt-item");
    const tokenOpt = [...opts].find((o) => o.querySelector(".opt-title").textContent.toLowerCase().includes("token"));
    tokenOpt.click();
    container.querySelector(".btn-next").click(); // step 4
    const tokenInput = container.querySelector("[data-field='token']");
    tokenInput.value = "test-token";
    container.querySelector(".btn-connect").click();
    await new Promise((r) => setTimeout(r, 0));
    return daemon.addMcpServer.mock.calls[0][0];
  }

  it("Slack (catalog, network) descriptor has id, nested auth.type, egress=network", async () => {
    const container = makeContainer();
    const descriptor = await buildDescriptorViaConnect(container, 0);
    expect(descriptor.id).toBe("slack");
    expect(descriptor.auth).toEqual({ type: "token" });
    expect(descriptor.egress).toBe("network");
    expect(descriptor.transport).toBe("http");
  });

  it("token-auth descriptor wires secret_ref to the Keychain account", async () => {
    const container = makeContainer();
    const descriptor = await buildDescriptorViaConnect(container, 0); // Slack via token
    expect(descriptor.secret_ref).toBe("mcp.slack.token");
  });

  it("Local Files (catalog, local/stdio) descriptor has egress=local", async () => {
    const container = makeContainer();
    const descriptor = await buildDescriptorViaConnect(container, 2);
    expect(descriptor.id).toBe("files");
    expect(descriptor.egress).toBe("local");
    expect(descriptor.transport).toBe("stdio");
    expect(Array.isArray(descriptor.args)).toBe(true);
    expect(typeof descriptor.env).toBe("object");
    expect(Array.isArray(descriptor.env)).toBe(false);
  });

  it("catalog pick seeds authType from catalog entry auth field", () => {
    const container = makeContainer();
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    // Click Slack (auth: "oauth")
    const items = container.querySelectorAll(".cat-item");
    items[0].click();
    // Advance to step 3
    container.querySelector(".btn-next").click(); // step 2
    fillUrl(container);
    container.querySelector(".btn-next").click(); // step 3
    const opts = container.querySelectorAll(".opt-item");
    const oauthOpt = [...opts].find((o) => o.querySelector(".opt-title").textContent.toLowerCase().includes("oauth"));
    // The OAuth option should be selected (state.authType was seeded to "oauth")
    expect(oauthOpt.classList.contains("selected")).toBe(true);
  });

  it("Custom stdio descriptor has args as array and env as object", async () => {
    const container = makeContainer();
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    const items = container.querySelectorAll(".cat-item");
    items[4].click(); // Custom
    container.querySelector(".btn-next").click(); // step 2
    // Select stdio
    const stdioRadio = container.querySelector("[data-transport='stdio']");
    stdioRadio.click();
    // Fill in command and args
    const cmdField = container.querySelector("[data-field='command']");
    cmdField.value = "npx my-server";
    const argsField = container.querySelector("[data-field='args']");
    argsField.value = "--foo bar";
    const envField = container.querySelector("[data-field='env']");
    envField.value = "FOO=bar\nBAZ=qux";
    container.querySelector(".btn-next").click(); // step 3
    // Select token auth
    const opts = container.querySelectorAll(".opt-item");
    const tokenOpt = [...opts].find((o) => o.querySelector(".opt-title").textContent.toLowerCase().includes("token"));
    tokenOpt.click();
    container.querySelector(".btn-next").click(); // step 4
    const tokenInput = container.querySelector("[data-field='token']");
    tokenInput.value = "test-token";
    container.querySelector(".btn-connect").click();
    await new Promise((r) => setTimeout(r, 0));
    const descriptor = daemon.addMcpServer.mock.calls[0][0];
    expect(Array.isArray(descriptor.args)).toBe(true);
    expect(descriptor.args).toEqual(["--foo", "bar"]);
    expect(typeof descriptor.env).toBe("object");
    expect(Array.isArray(descriptor.env)).toBe(false);
    expect(descriptor.env).toEqual({ FOO: "bar", BAZ: "qux" });
    expect(descriptor.egress).toBe("local");
  });
});

// ---------------------------------------------------------------------------
// Step 2 gate — Custom required fields
// ---------------------------------------------------------------------------
describe("Step 2 gate — Custom required fields", () => {
  function setupCustomStep2(container) {
    showAddConnection(container, { onDone: vi.fn(), onCancel: vi.fn() });
    const items = container.querySelectorAll(".cat-item");
    items[4].click(); // Custom
    container.querySelector(".btn-next").click(); // go to step 2
  }

  it("Custom HTTP with empty URL cannot advance from step 2", () => {
    const container = makeContainer();
    setupCustomStep2(container);
    // HTTP is default; URL is empty — click next
    const urlField = container.querySelector("[data-field='url']");
    expect(urlField).not.toBeNull();
    urlField.value = ""; // ensure empty
    container.querySelector(".btn-next").click();
    // Should still be on step 2
    expect(container.querySelector("[data-step='2']")).not.toBeNull();
    expect(container.querySelector("[data-step='3']")).toBeNull();
  });

  it("Custom HTTP with non-empty URL advances from step 2", () => {
    const container = makeContainer();
    setupCustomStep2(container);
    const urlField = container.querySelector("[data-field='url']");
    urlField.value = "https://example.com/mcp";
    container.querySelector(".btn-next").click();
    expect(container.querySelector("[data-step='3']")).not.toBeNull();
  });

  it("Custom stdio with empty command cannot advance from step 2", () => {
    const container = makeContainer();
    setupCustomStep2(container);
    // Switch to stdio
    const stdioRadio = container.querySelector("[data-transport='stdio']");
    stdioRadio.click();
    const cmdField = container.querySelector("[data-field='command']");
    expect(cmdField).not.toBeNull();
    cmdField.value = ""; // ensure empty
    container.querySelector(".btn-next").click();
    expect(container.querySelector("[data-step='2']")).not.toBeNull();
    expect(container.querySelector("[data-step='3']")).toBeNull();
  });

  it("Custom stdio with non-empty command advances from step 2", () => {
    const container = makeContainer();
    setupCustomStep2(container);
    const stdioRadio = container.querySelector("[data-transport='stdio']");
    stdioRadio.click();
    const cmdField = container.querySelector("[data-field='command']");
    cmdField.value = "npx my-server";
    container.querySelector(".btn-next").click();
    expect(container.querySelector("[data-step='3']")).not.toBeNull();
  });
});
