/**
 * Remote SSH frontend plugin for QwenPaw
 *
 * Registers:
 * - Tool renderers for remote_connect, remote_disconnect, remote_list, remote_exec
 * - Remote SSH profile management page
 *
 * Uses window.QwenPaw plugin API
 */

declare const __REMOTE_PLUGIN_VERSION__: string;

const REMOTE_PLUGIN_BUILD_ID = __REMOTE_PLUGIN_VERSION__;
const REMOTE_PLUGIN_ID = "remote";

/**
 * Last session id the host handed to a chat request transform.
 *
 * Some host builds expose neither `host.getCurrentSessionId()` nor a session
 * global, but they do pass `sessionId` to `chat.requestPayload` transforms.
 */
let cachedSessionId: string | null = null;

function buildPlugin() {
  const runtime = window as any;
  if (runtime.__remotePluginInitializedBuild === REMOTE_PLUGIN_BUILD_ID) return;
  runtime.__remotePluginInitialized = true;
  runtime.__remotePluginInitializedBuild = REMOTE_PLUGIN_BUILD_ID;

  const host = runtime.QwenPaw.host;
  const qwenpaw = runtime.QwenPaw;
  const { React, antd, antdIcons } = host;
  const {
    Card,
    Tag,
    Typography,
    Space,
    Button,
    Input,
    Select,
    Form,
    Modal,
    Spin,
    Alert,
    Switch,
    message: antdMessage,
    List,
    Badge,
    Popconfirm,
    Empty,
    Tooltip,
    Popover,
  } = antd;
  const { Text, Title, Paragraph } = Typography;
  const { useState, useEffect, useCallback } = React;
  const {
    CloudOutlined,
    LinkOutlined,
    DisconnectOutlined,
    CodeOutlined,
    ReloadOutlined,
    PlusOutlined,
    DeleteOutlined,
    EditOutlined,
    LoadingOutlined,
    LaptopOutlined,
    ThunderboltOutlined,
    InfoCircleOutlined,
    HeartOutlined,
    FolderOutlined,
    SafetyOutlined,
  } = antdIcons || {};

  // ── Language Detection ─────────────────────────────────────────────

  function useZh(): boolean {
    // `useLocale` is the documented host hook ("zh" | "en").
    const locale = typeof host.useLocale === "function" ? host.useLocale() : "";
    return String(locale).toLowerCase().startsWith("zh");
  }

  // ── Helpers ──────────────────────────────────────────────────────────

  function renderIcon(icon: any, fallback: string = "•") {
    if (icon) return React.createElement(icon);
    return React.createElement("span", null, fallback);
  }

  /**
   * Unwrap the various shapes a tool result can arrive in.
   *
   * `chat.toolRender` hands the renderer `React.FC<Record<string, unknown>>`,
   * so the payload may be the raw string, a tool-call object, or a wrapper
   * such as `{ result }` / `{ output }` / `{ data }`.
   */
  function normalizeToolData(data: any): any {
    let value = data;
    const seen = new Set<any>();
    while (
      value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      !seen.has(value)
    ) {
      seen.add(value);
      if (value.result !== undefined) {
        value = value.result;
        continue;
      }
      if (value.toolResult !== undefined) {
        value = value.toolResult;
        continue;
      }
      if (value.output !== undefined) {
        value = value.output;
        continue;
      }
      // Do not unwrap a ToolResponse-like payload that carries `content`.
      if (value.data !== undefined && !("content" in value)) {
        value = value.data;
        continue;
      }
      break;
    }
    return value;
  }

  let toolShapeLogged = false;

  /** Log the real prop shape once, so future mismatches are diagnosable. */
  function logToolShape(props: any, toolName: string, extracted: string) {
    if (toolShapeLogged) return;
    toolShapeLogged = true;
    console.debug(
      "[Remote] toolRender props for",
      toolName,
      props,
      "keys:",
      props && typeof props === "object" ? Object.keys(props) : typeof props,
      "extracted:",
      extracted.slice(0, 300),
    );
  }

  function parseToolArgs(data: any): Record<string, any> {
    const value = normalizeToolData(data);
    const content0 = Array.isArray(value?.content) ? value.content[0] : null;
    const candidates = [
      value?.arguments,
      value?.input,
      value?.args,
      content0?.arguments,
      content0?.input,
      content0?.data?.arguments,
    ];

    for (const candidate of candidates) {
      if (candidate === undefined || candidate === null || candidate === "") {
        continue;
      }
      if (typeof candidate === "string") {
        try {
          return JSON.parse(candidate);
        } catch {
          continue;
        }
      }
      if (typeof candidate === "object") return candidate;
    }
    return {};
  }

  const TOOL_OUTPUT_KEYS = ["text", "output", "result", "stdout", "message", "value"];
  const TOOL_OUTPUT_CONTAINERS = ["content", "data", "blocks", "toolResult"];

  /** True for a block that carries the tool CALL (arguments), not its result. */
  function isToolCallRecord(node: any): boolean {
    if (!node || typeof node !== "object" || Array.isArray(node)) return false;
    const hasArgs = "arguments" in node || "call_id" in node;
    const hasOutput = TOOL_OUTPUT_KEYS.some((key) => key in node);
    return hasArgs && !hasOutput;
  }

  /**
   * Depth-first search for the tool's output text.
   *
   * The host hands over a Msg-like payload (`plugin_call_output`) whose
   * content mixes the call record with the result, and the exact key varies
   * between host builds — so known keys are tried first and the longest
   * non-argument string is used as a last resort.
   */
  function deepFindOutput(
    node: any,
    depth = 0,
    seen = new Set<any>(),
    longest = { value: "" },
  ): string {
    if (node === null || node === undefined || depth > 8) return "";
    if (typeof node === "string") return node;
    if (typeof node === "number" || typeof node === "boolean") return String(node);
    if (Array.isArray(node)) {
      for (const item of node) {
        const found = deepFindOutput(item, depth + 1, seen, longest);
        if (found) return found;
      }
      return "";
    }
    if (typeof node !== "object") return "";
    if (seen.has(node)) return "";
    seen.add(node);
    if (isToolCallRecord(node)) return "";

    for (const key of TOOL_OUTPUT_KEYS) {
      const child = node[key];
      if (child === undefined || child === null) continue;
      const found = deepFindOutput(child, depth + 1, seen, longest);
      if (found) return found;
    }
    for (const key of TOOL_OUTPUT_CONTAINERS) {
      const child = node[key];
      if (child === undefined || child === null) continue;
      const found = deepFindOutput(child, depth + 1, seen, longest);
      if (found) return found;
    }

    // Last resort: remember the longest string that is not a tool argument.
    for (const value of Object.values(node)) {
      if (typeof value === "string" && value.length > longest.value.length) {
        longest.value = value;
      }
    }
    return "";
  }

  /** Full output text of a tool result, whatever shape it arrives in. */
  function parseToolOutput(data: any): string {
    const value = normalizeToolData(data);
    if (value === undefined || value === null) return "";

    const found = deepFindOutput(value);
    if (found) return found;
    if (typeof value === "string") return value;

    const longest = { value: "" };
    deepFindOutput(value, 0, new Set<any>(), longest);
    if (longest.value) return longest.value;

    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  function getSessionId(): string | null {
    // Documented host getter first, then the globals older hosts expose, then
    // the id captured from chat request payloads.
    const hostSession =
      typeof host.getCurrentSessionId === "function"
        ? host.getCurrentSessionId()
        : null;
    if (hostSession) return String(hostSession);

    const runtime = window as any;
    const legacy = runtime.currentSessionId || runtime.sessionId;
    if (legacy) return String(legacy);

    // No session is a normal state on pages without an open chat. The plugin
    // must never invent a shared fallback id, because that would let
    // unrelated sessions share (and disconnect) one SSH connection.
    return cachedSessionId;
  }

  /**
   * Session id for user-initiated actions.
   *
   * Reports once when unavailable; polling loops must use getSessionId()
   * directly so a missing session cannot flood the UI with errors.
   */
  function requireSessionId(
    zh: boolean,
    resolved?: string | null,
  ): string | null {
    const sessionId = resolved ?? getSessionId();
    if (!sessionId) {
      antdMessage.error(
        zh
          ? "当前没有打开的会话，请先进入一个对话再操作。"
          : "No active chat session. Open a chat first.",
      );
    }
    return sessionId;
  }

  /** True when the backend refused an untrusted or mismatched host key. */
  function isHostKeyError(message: string): boolean {
    return /known_hosts|host key/i.test(message);
  }

  /**
   * Ask the backend which SSH connection belongs to this caller.
   *
   * The Remote SSH settings route has no "current session", so this
   * owner-scoped lookup is how the UI learns both the connection and the
   * session id it needs for later scoped calls.
   */
  async function fetchActiveConnection(): Promise<any | null> {
    try {
      const data = await apiFetch("/remote/connections/active");
      const sessionId = data?.session_id ? String(data.session_id) : null;
      if (sessionId) cachedSessionId = sessionId;
      return data;
    } catch (e) {
      console.debug("[Remote] Active connection lookup failed:", e);
      return null;
    }
  }

  /**
   * Connect through a saved profile.
   *
   * On an untrusted host key the offer to trust it once is shown and the
   * attempt is repeated with an explicit override; the key is then recorded in
   * the plugin's known_hosts so later connections stay strictly verified.
   */
  async function connectViaProfile(
    profileId: string,
    sessionId: string,
    zh: boolean,
  ) {
    const body: Record<string, unknown> = { session_id: sessionId };
    try {
      return await apiFetch(`/remote/profiles/${profileId}/connect`, {
        method: "POST",
        body: JSON.stringify(body),
      });
    } catch (e: any) {
      const message = e?.message || String(e);
      // A key *mismatch* is never auto-trusted: it needs manual verification.
      if (!isHostKeyError(message) || /mismatch/i.test(message)) throw e;
      const trust = await new Promise<boolean>((resolve) => {
        Modal.confirm({
          title: zh ? "主机密钥未受信任" : "Untrusted host key",
          content: zh
            ? "该主机的密钥不在 known_hosts 中。请先用带外方式核对主机指纹（例如 ssh-keyscan）。确认信任后，密钥会记入插件自己的 known_hosts，之后的连接仍会严格校验。"
            : "This host key is not in known_hosts. Verify the fingerprint out of band first (for example with ssh-keyscan). Once trusted, the key is stored in the plugin's known_hosts and later connections stay strictly verified.",
          okText: zh ? "信任并重试" : "Trust and retry",
          cancelText: zh ? "取消" : "Cancel",
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        });
      });
      if (!trust) throw e;
      return apiFetch(`/remote/profiles/${profileId}/connect`, {
        method: "POST",
        body: JSON.stringify({ ...body, accept_new_host_key: true }),
      });
    }
  }

  async function apiFetch(path: string, options: RequestInit = {}) {
    // The request is built explicitly instead of using host.fetch: some host
    // builds ignore options.method and silently send every call as GET,
    // which turns POST/PATCH into list endpoints or 405 responses.
    const url =
      typeof host.getApiUrl === "function" ? host.getApiUrl(path) : path;
    const token =
      typeof host.getApiToken === "function" ? host.getApiToken() : null;
    const agentId =
      typeof host.getSelectedAgentId === "function"
        ? host.getSelectedAgentId()
        : null;

    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      // The backend uses this to enforce per-caller session ownership.
      ...(agentId ? { "X-Agent-Id": String(agentId) } : {}),
      ...((options.headers as Record<string, string>) || {}),
    };

    const method = options.method || "GET";
    const res = await fetch(url, { ...options, method, headers });
    if (!res.ok) {
      const body = await res.text();
      console.debug("[Remote] API error", method, url, res.status, body);
      throw new Error(`${res.status}: ${body}`);
    }
    return res.json();
  }

  const theme = {
    text: "var(--ant-color-text, CanvasText)",
    secondaryText:
      "var(--ant-color-text-secondary, color-mix(in srgb, CanvasText 62%, transparent))",
    border:
      "var(--ant-color-border, color-mix(in srgb, CanvasText 18%, transparent))",
    bgContainer: "var(--ant-color-bg-container, Canvas)",
    bgElevated:
      "var(--ant-color-bg-elevated, var(--ant-color-bg-container, Canvas))",
    fillSecondary:
      "var(--ant-color-fill-secondary, color-mix(in srgb, CanvasText 8%, transparent))",
    success: "var(--ant-color-success, #52c41a)",
    successBorder:
      "var(--ant-color-success-border, var(--ant-color-success, #52c41a))",
    successBg: "var(--ant-color-success-bg, transparent)",
    error: "var(--ant-color-error, #ff4d4f)",
    errorBorder:
      "var(--ant-color-error-border, var(--ant-color-error, #ff4d4f))",
    errorBg: "var(--ant-color-error-bg, transparent)",
    primary: "var(--ant-color-primary, #1677ff)",
    primaryText: "var(--ant-color-white, #fff)",
    shadow:
      "var(--ant-box-shadow-secondary, 0 8px 24px rgba(0, 0, 0, 0.18))",
  };

  // ── Tool Renderers ──────────────────────────────────────────────────

  /**
   * Wraps a tool card so its body is collapsed by default; the user expands
   * what they need. Content that already fits stays uncropped and shows no
   * toggle.
   */
  function CollapsibleToolBody({ children }: { children: any }) {
    const [open, setOpen] = useState(false);
    const [overflows, setOverflows] = useState(false);
    const bodyRef = React.useRef(null as any);

    // `scrollHeight` measures the full content even while clamped.
    useEffect(() => {
      const element = bodyRef.current;
      if (element) setOverflows(element.scrollHeight > 132);
    }, [open]);

    return React.createElement(
      "div",
      null,
      React.createElement(
        "div",
        {
          ref: bodyRef,
          style: {
            maxHeight: open || !overflows ? undefined : 120,
            overflow: open || !overflows ? undefined : "hidden",
          },
        },
        children,
      ),
      overflows
        ? React.createElement(
            Button,
            {
              type: "link",
              size: "small",
              style: { padding: 0, height: "auto", fontSize: 12 },
              onClick: () => setOpen(!open),
            },
            open ? "收起 / Collapse" : "展开 / Expand",
          )
        : null,
    );
  }

  function RemoteConnectRender({ data }: { data: any }) {
    const output = parseToolOutput(data);
    const args = parseToolArgs(data);
    const isSuccess = output.includes("Connected to");
    const isFailure = output.includes("Error") || output.includes("failed");

    return React.createElement(
      Card,
      {
        size: "small",
        style: {
          marginTop: 8,
          borderLeft: `3px solid ${isSuccess ? "#52c41a" : isFailure ? "#ff4d4f" : "#1890ff"}`,
        },
      },
      React.createElement(
        Space,
        { direction: "vertical", style: { width: "100%" } },
        React.createElement(
          Space,
          null,
          React.createElement(LinkOutlined || "\u{1F517}"),
          React.createElement(Text, { strong: true }, "Remote SSH Connect"),
          args.host
            ? React.createElement(
                Tag,
                { color: "blue" },
                `${args.username}@${args.host}:${args.port || 22}`,
              )
            : null,
        ),
        React.createElement(
          Text,
          {
            type: isFailure ? "danger" : "success",
            style: { whiteSpace: "pre-wrap" },
          },
          output,
        ),
      ),
    );
  }

  function RemoteDisconnectRender({ data }: { data: any }) {
    const output = parseToolOutput(data);
    const isSuccess = output.includes("Disconnected");

    return React.createElement(
      Card,
      {
        size: "small",
        style: {
          marginTop: 8,
          borderLeft: `3px solid ${isSuccess ? "#52c41a" : "#faad14"}`,
        },
      },
      React.createElement(
        Space,
        { direction: "vertical", style: { width: "100%" } },
        React.createElement(
          Space,
          null,
          React.createElement(DisconnectOutlined || "\u{1F50C}"),
          React.createElement(Text, { strong: true }, "Remote SSH Disconnect"),
        ),
        React.createElement(
          Text,
          { style: { whiteSpace: "pre-wrap" } },
          output,
        ),
      ),
    );
  }

  function RemoteListRender({ data }: { data: any }) {
    const output = parseToolOutput(data);
    const hasConnection = output.includes("Active SSH connection");

    return React.createElement(
      Card,
      {
        size: "small",
        style: {
          marginTop: 8,
          borderLeft: `3px solid ${hasConnection ? "#52c41a" : "#d9d9d9"}`,
        },
      },
      React.createElement(
        Space,
        { direction: "vertical", style: { width: "100%" } },
        React.createElement(
          Space,
          null,
          React.createElement(CloudOutlined || "☁"),
          React.createElement(Text, { strong: true }, "Remote SSH Status"),
          hasConnection
            ? React.createElement(Badge, {
                status: "success",
                text: "Connected",
              })
            : React.createElement(Badge, {
                status: "default",
                text: "Not Connected",
              }),
        ),
        React.createElement(
          Text,
          { style: { whiteSpace: "pre-wrap" } },
          output,
        ),
      ),
    );
  }

  function RemoteExecRender({ data }: { data: any }) {
    const output = parseToolOutput(data);
    const args = parseToolArgs(data);
    const isRemote = output.includes("[remote:");
    const isFailure = output.includes("failed with exit code");

    return React.createElement(
      Card,
      {
        size: "small",
        style: {
          marginTop: 8,
          borderLeft: `3px solid ${isFailure ? "#ff4d4f" : isRemote ? "#722ed1" : "#d9d9d9"}`,
        },
      },
      React.createElement(
        Space,
        { direction: "vertical", style: { width: "100%" } },
        React.createElement(
          Space,
          null,
          React.createElement(CodeOutlined || ">_"),
          React.createElement(Text, { strong: true }, "Remote Command"),
        ),
        args.command
          ? React.createElement(
              "pre",
              {
                style: {
                  margin: 0,
                  padding: "6px 8px",
                  background: "rgba(0,0,0,0.03)",
                  border: "1px solid rgba(0,0,0,0.06)",
                  borderRadius: 4,
                  fontSize: 12,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-all",
                },
              },
              `$ ${args.command}`,
            )
          : null,
        React.createElement(
          "pre",
          {
            style: {
              margin: 0,
              padding: "8px 12px",
              background: "#f5f5f5",
              borderRadius: 4,
              fontSize: 12,
              maxHeight: 300,
              overflow: "auto",
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
            },
          },
          output,
        ),
      ),
    );
  }

  function RemoteReconnectRender({ data }: { data: any }) {
    const output = parseToolOutput(data);
    const isSuccess = output.includes("Reconnected to");
    const isFailure = output.includes("failed") || output.includes("Error");

    return React.createElement(
      Card,
      {
        size: "small",
        style: {
          marginTop: 8,
          borderLeft: `3px solid ${isSuccess ? "#52c41a" : isFailure ? "#ff4d4f" : "#faad14"}`,
        },
      },
      React.createElement(
        Space,
        { direction: "vertical", style: { width: "100%" } },
        React.createElement(
          Space,
          null,
          React.createElement(ReloadOutlined || "\u{21BB}"),
          React.createElement(Text, { strong: true }, "Remote SSH Reconnect"),
        ),
        React.createElement(
          Text,
          { type: isFailure ? "danger" : "success", style: { whiteSpace: "pre-wrap" } },
          output,
        ),
      ),
    );
  }

  function RemoteInfoRender({ data }: { data: any }) {
    const zh = useZh();
    const output = parseToolOutput(data);
    const hasInfo = output.includes("Remote Environment");

    return React.createElement(
      Card,
      {
        size: "small",
        style: {
          marginTop: 8,
          borderLeft: "3px solid #1677ff",
        },
      },
      React.createElement(
        Space,
        { direction: "vertical", style: { width: "100%" } },
        React.createElement(
          Space,
          null,
          React.createElement(InfoCircleOutlined || CloudOutlined || "\u{2139}"),
          React.createElement(Text, { strong: true }, zh ? "远程设备信息" : "Remote Machine Info"),
        ),
        React.createElement(
          "pre",
          {
            style: {
              margin: 0,
              padding: "8px 12px",
              background: "#f5f5f5",
              borderRadius: 4,
              fontSize: 12,
              maxHeight: 400,
              overflow: "auto",
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
            },
          },
          output,
        ),
      ),
    );
  }

  function RemoteHealthRender({ data }: { data: any }) {
    const zh = useZh();
    const output = parseToolOutput(data);
    const isDegraded = output.includes("Unstable");
    const isStale = output.includes("Disconnected");
    const isConnected = output.includes("Status: Connected");
    const borderColor = isStale ? "#ff4d4f" : isDegraded ? "#faad14" : isConnected ? "#52c41a" : "#d9d9d9";
    return React.createElement(
      Card, { size: "small", style: { marginTop: 8, borderLeft: `3px solid ${borderColor}` } },
      React.createElement(Space, { direction: "vertical", style: { width: "100%" } },
        React.createElement(Space, null,
          React.createElement(HeartOutlined || "\u{2764}"),
          React.createElement(Text, { strong: true }, zh ? "连接健康状态" : "Connection Health"),
        ),
        React.createElement("pre", { style: { margin: 0, padding: "8px 12px", background: "#f5f5f5", borderRadius: 4, fontSize: 12, maxHeight: 200, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-all" } }, output),
      ),
    );
  }

  function RemoteSetCwdRender({ data }: { data: any }) {
    const zh = useZh();
    const output = parseToolOutput(data);
    const isSuccess = output.includes("set to:");
    const isFailure = output.includes("Error");
    return React.createElement(
      Card, { size: "small", style: { marginTop: 8, borderLeft: `3px solid ${isSuccess ? "#52c41a" : isFailure ? "#ff4d4f" : "#d9d9d9"}` } },
      React.createElement(Space, { direction: "vertical", style: { width: "100%" } },
        React.createElement(Space, null,
          React.createElement(FolderOutlined || "\u{1F4C1}"),
          React.createElement(Text, { strong: true }, zh ? "设置工作目录" : "Set Working Directory"),
        ),
        React.createElement(Text, { type: isFailure ? "danger" : "success", style: { whiteSpace: "pre-wrap" } }, output),
      ),
    );
  }

  function RemoteSudoRender({ data }: { data: any }) {
    const zh = useZh();
    const output = parseToolOutput(data);
    const isFailure = output.includes("failed with exit code") || output.includes("Error");
    return React.createElement(
      Card, { size: "small", style: { marginTop: 8, borderLeft: `3px solid ${isFailure ? "#ff4d4f" : "#faad14"}` } },
      React.createElement(Space, { direction: "vertical", style: { width: "100%" } },
        React.createElement(Space, null,
          React.createElement(SafetyOutlined || "\u{1F6E1}"),
          React.createElement(Text, { strong: true }, zh ? "Sudo 命令" : "Sudo Command"),
        ),
        React.createElement("pre", { style: { margin: 0, padding: "8px 12px", background: "#f5f5f5", borderRadius: 4, fontSize: 12, maxHeight: 300, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-all" } }, output),
      ),
    );
  }

  // ── Remote Management Page ──────────────────────────────────────────

  function RemotePage() {
    const zh = useZh();
    const [profiles, setProfiles] = useState([] as any[]);
    const [jumpHosts, setJumpHosts] = useState([] as any[]);
    const [activeProfileId, setActiveProfileId] = useState("");
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");
    const [modalOpen, setModalOpen] = useState(false);
    const [jumpModalOpen, setJumpModalOpen] = useState(false);
    const [editingProfile, setEditingProfile] = useState(null as any | null);
    const [editingJumpHost, setEditingJumpHost] = useState(null as any | null);
    const [saving, setSaving] = useState(false);
    const [savingJumpHost, setSavingJumpHost] = useState(false);
    const [connectingId, setConnectingId] = useState(null as string | null);
    const [cwdValue, setCwdValue] = useState("/");
    const [cwdEditing, setCwdEditing] = useState(false);
    const [form] = Form.useForm();
    const [jumpForm] = Form.useForm();

    // Populated from the owner-scoped active-connection lookup; the settings
    // route cannot ask the host for a "current session".
    const [sessionId, setSessionId] = useState(getSessionId() || "");
    const [pendingProfileId, setPendingProfileId] = useState("");

    const fetchData = useCallback(async () => {
      setLoading(true);
      setError("");
      try {
        // Profiles and jump hosts are host-level configuration. The active
        // connection lookup supplies both the "connected" badge and the
        // session id used by later scoped calls.
        const [active, profileData, jumpHostData] = await Promise.all([
          fetchActiveConnection(),
          apiFetch("/remote/profiles"),
          apiFetch("/remote/jump-hosts"),
        ]);
        const activeConnection = active?.connection || null;
        setSessionId(
          active?.session_id
            ? String(active.session_id)
            : getSessionId() || "",
        );
        setProfiles(profileData.profiles || []);
        setActiveProfileId(activeConnection?.profile_id || "");
        setPendingProfileId(active?.pending_profile_id || "");
        setJumpHosts(jumpHostData.jump_hosts || []);
        setCwdValue(activeConnection?.default_cwd || "/");
      } catch (e: any) {
        const errMsg = e.message || String(e);
        console.error("[Remote] Failed to fetch data:", e);
        // Inline only: this runs on a polling interval, so it must not toast.
        setError(errMsg);
      } finally {
        setLoading(false);
      }
    }, []);

    useEffect(() => {
      fetchData();
      const interval = setInterval(fetchData, 10000);
      return () => clearInterval(interval);
    }, [fetchData]);

    const handleSave = async (values: any) => {
      setSaving(true);
      try {
        await apiFetch(
          editingProfile
            ? `/remote/profiles/${editingProfile.id}`
            : "/remote/profiles",
          {
            method: editingProfile ? "PUT" : "POST",
            body: JSON.stringify(values),
          },
        );
        antdMessage.success(
          editingProfile
            ? "Connection profile updated"
            : "Connection profile saved",
        );
        setModalOpen(false);
        setEditingProfile(null);
        form.resetFields();
        fetchData();
      } catch (e: any) {
        antdMessage.error(`Save failed: ${e.message}`);
      } finally {
        setSaving(false);
      }
    };

    const openNewProfileModal = () => {
      setEditingProfile(null);
      form.resetFields();
      setModalOpen(true);
    };

    const openEditProfileModal = (profile: any) => {
      setEditingProfile(profile);
      form.setFieldsValue({
        name: profile.name,
        host: profile.host,
        port: profile.port,
        username: profile.username,
        password: "",
        key_path: profile.key_path,
        passphrase: "",
        sudo_password: "",
        jump_host_id: profile.jump_host_id || "",
        default_cwd: profile.default_cwd || "",
        accept_new_host_key: Boolean(profile.accept_new_host_key),
      });
      setModalOpen(true);
    };

    const handleSaveJumpHost = async (values: any) => {
      setSavingJumpHost(true);
      try {
        await apiFetch(
          editingJumpHost
            ? `/remote/jump-hosts/${editingJumpHost.id}`
            : "/remote/jump-hosts",
          {
            method: editingJumpHost ? "PUT" : "POST",
            body: JSON.stringify(values),
          },
        );
        antdMessage.success(
          editingJumpHost ? "Jump host updated" : "Jump host saved",
        );
        setJumpModalOpen(false);
        setEditingJumpHost(null);
        jumpForm.resetFields();
        fetchData();
      } catch (e: any) {
        antdMessage.error(`Save jump host failed: ${e.message}`);
      } finally {
        setSavingJumpHost(false);
      }
    };

    const openNewJumpHostModal = () => {
      setEditingJumpHost(null);
      jumpForm.resetFields();
      setJumpModalOpen(true);
    };

    const openEditJumpHostModal = (jumpHost: any) => {
      setEditingJumpHost(jumpHost);
      jumpForm.setFieldsValue({
        name: jumpHost.name,
        host: jumpHost.host,
        port: jumpHost.port,
        username: jumpHost.username,
        password: "",
        key_path: jumpHost.key_path,
        passphrase: "",
      });
      setJumpModalOpen(true);
    };

    const handleDeleteJumpHost = async (jumpHostId: string) => {
      try {
        await apiFetch(`/remote/jump-hosts/${jumpHostId}`, {
          method: "DELETE",
        });
        antdMessage.success("Jump host deleted");
        fetchData();
      } catch (e: any) {
        antdMessage.error(`Delete jump host failed: ${e.message}`);
      }
    };

    const handleToggleConnect = async (profile: any) => {
      if (profile.id === activeProfileId) {
        // Disconnect needs the session id of the live connection.
        const activeSessionId = requireSessionId(zh, sessionId);
        if (!activeSessionId) return;
        try {
          await apiFetch(`/remote/connections/${activeSessionId}`, {
            method: "DELETE",
          });
          antdMessage.success("Disconnected");
          fetchData();
        } catch (e: any) {
          antdMessage.error(`Disconnect failed: ${e.message}`);
        }
        return;
      }

      // Connect. A brand-new chat has no session id yet, so an empty id is
      // sent and the backend defers the connect to the chat's first turn.
      setConnectingId(profile.id);
      try {
        const result = await connectViaProfile(profile.id, sessionId, zh);
        if (result?.pending) {
          antdMessage.info(
            zh
              ? `已记录：本对话发出第一条消息后自动连接 ${profile.name}`
              : `Recorded: will connect to ${profile.name} on this chat's first message`,
          );
        } else {
          antdMessage.success(
            zh
              ? `已连接到 ${profile.name}`
              : `Connected to ${profile.name}`,
          );
        }

        if (result?.sudo_needs_password && sessionId) {
          const sudoPwd = prompt(
            zh ? "未配置 sudo 密码，需要时请输入（留空跳过）：" : "Sudo password is not configured. Enter it if needed (empty to skip):"
          );
          if (sudoPwd) {
            await apiFetch(`/remote/connections/${sessionId}/sudo`, {
              method: "POST",
              body: JSON.stringify({ password: sudoPwd, enabled: true }),
            });
          }
        }
        fetchData();
      } catch (e: any) {
        antdMessage.error(zh ? `连接失败: ${e.message}` : `Connection failed: ${e.message}`);
      } finally {
        setConnectingId(null);
      }
    };

    const handleDelete = async (profileId: string) => {
      try {
        await apiFetch(`/remote/profiles/${profileId}`, {
          method: "DELETE",
        });
        antdMessage.success("Profile deleted");
        fetchData();
      } catch (e: any) {
        antdMessage.error(`Delete failed: ${e.message}`);
      }
    };

    const handleSetCwd = async (nextCwd?: string) => {
      if (!requireSessionId(zh, sessionId)) return;
      const targetCwd = (nextCwd ?? cwdValue).trim();
      if (!targetCwd) return;
      setCwdEditing(true);
      try {
        await apiFetch(`/remote/connections/${sessionId}/cwd`, {
          method: "PUT",
          body: JSON.stringify({ cwd: targetCwd, verify: true }),
        });
        setCwdValue(targetCwd);
        // Sync to profile config if connected via profile
        if (activeProfileId) {
          await apiFetch(`/remote/profiles/${activeProfileId}/cwd`, {
            method: "PATCH",
            body: JSON.stringify({ cwd: targetCwd }),
          }).catch(() => {});
        }
        antdMessage.success(zh ? "工作目录已更新" : "Working directory updated");
      } catch (e: any) {
        antdMessage.error(zh ? `失败: ${e.message}` : `Failed: ${e.message}`);
      } finally {
        setCwdEditing(false);
      }
    };

    const isConnected = (profileId: string) => profileId === activeProfileId;

    return React.createElement(
      "div",
      { style: { padding: 24, maxWidth: 900, margin: "0 auto" } },
      // Header
      React.createElement(
        Space,
        {
          style: {
            marginBottom: 16,
            width: "100%",
            justifyContent: "space-between",
          },
        },
        React.createElement(
          Title,
          { level: 4, style: { margin: 0 } },
          zh ? "远程 SSH" : "Remote SSH",
        ),
        React.createElement(
          Space,
          null,
          React.createElement(
            Button,
            { icon: renderIcon(ReloadOutlined), onClick: fetchData },
            zh ? "刷新" : "Refresh",
          ),
          React.createElement(
            Button,
            {
              icon: renderIcon(PlusOutlined),
              onClick: openNewJumpHostModal,
            },
            zh ? "新建跳板机" : "New Jump Host",
          ),
          React.createElement(
            Button,
            {
              type: "primary",
              icon: renderIcon(PlusOutlined),
              onClick: openNewProfileModal,
            },
            zh ? "新建连接" : "New Connection",
          ),
        ),
      ),
      // Info alert + error alert if any
      React.createElement(Alert, {
        type: "info",
        showIcon: true,
        style: { marginBottom: 16 },
        message:
          zh
            ? "在这里保存 SSH 连接配置。使用开关连接或断开设备；同一时间只能有一个连接处于活跃状态。连接后，当前对话中的 shell 命令会在远程设备上执行。"
            : "Save connection profiles here. Toggle the switch to connect/disconnect. " +
              "Only one connection can be active at a time. " +
              "When connected, all shell commands in the current chat execute on the remote machine.",
      }),
      error
        ? React.createElement(Alert, {
            type: "error",
            showIcon: true,
            style: { marginBottom: 16 },
            message: zh ? "加载数据失败" : "Error loading data",
            description: error,
          })
        : null,
      React.createElement(
        Card,
        {
          size: "small",
          title: zh ? "跳板机" : "Jump Hosts",
          style: { marginBottom: 16 },
        },
        jumpHosts.length === 0
          ? React.createElement(
              Empty,
              {
                image: Empty.PRESENTED_IMAGE_SIMPLE,
                description: zh ? "暂无保存的跳板机。" : "No saved jump hosts.",
              },
            )
          : React.createElement(
              List,
              {
                dataSource: jumpHosts,
                renderItem: (jumpHost: any) =>
                  React.createElement(
                    "div",
                    {
                      style: {
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        padding: "8px 0",
                        borderBottom: "1px solid #f0f0f0",
                      },
                    },
                    React.createElement(
                      "div",
                      { style: { minWidth: 0 } },
                      React.createElement(
                        Text,
                        { strong: true },
                        jumpHost.name ||
                          `${jumpHost.username}@${jumpHost.host}`,
                      ),
                      React.createElement(
                        "div",
                        { style: { marginTop: 4 } },
                        React.createElement(
                          Text,
                          { type: "secondary", style: { fontSize: 12 } },
                          `${jumpHost.username}@${jumpHost.host}:${jumpHost.port}`,
                          jumpHost.key_path
                            ? `  |  ${zh ? "密钥" : "Key"}: ${jumpHost.key_path}`
                            : "",
                        ),
                      ),
                    ),
                    React.createElement(
                      Space,
                      null,
                      React.createElement(
                        Tooltip,
                        { title: zh ? "编辑此跳板机" : "Edit this jump host" },
                        React.createElement(Button, {
                          type: "text",
                          size: "small",
                          icon: renderIcon(EditOutlined),
                          onClick: () => openEditJumpHostModal(jumpHost),
                        }),
                      ),
                      React.createElement(
                        Popconfirm,
                        {
                          title: zh ? "删除此跳板机？" : "Delete this jump host?",
                          onConfirm: () => handleDeleteJumpHost(jumpHost.id),
                          okText: zh ? "删除" : "Delete",
                          cancelText: zh ? "取消" : "Cancel",
                          okButtonProps: { danger: true },
                        },
                        React.createElement(Button, {
                          type: "text",
                          danger: true,
                          size: "small",
                          icon: renderIcon(DeleteOutlined),
                        }),
                      ),
                    ),
                  ),
              },
            ),
      ),
      React.createElement(
        Title,
        { level: 5, style: { marginTop: 0 } },
        zh ? "设备" : "Devices",
      ),
      // Profile list
      loading
        ? React.createElement(Spin, {
            style: { display: "block", margin: "40px auto" },
          })
        : profiles.length === 0
          ? React.createElement(
              Card,
              null,
              React.createElement(
                Empty,
                {
                  description: React.createElement(
                    Paragraph,
                    { type: "secondary" },
                    zh ? "暂无保存的连接。点击“新建连接”添加一个。" : "No saved connections. Click 'New Connection' to add one.",
                  ),
                },
              ),
            )
          : React.createElement(
              List,
              {
                dataSource: profiles,
                renderItem: (profile: any) => {
                  const connected = isConnected(profile.id);
                  const isConnecting = connectingId === profile.id;

                  return React.createElement(
                    Card,
                    {
                      size: "small",
                      style: {
                        marginBottom: 8,
                        borderColor: connected ? "#52c41a" : undefined,
                      },
                    },
                    React.createElement(
                      "div",
                      {
                        style: {
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                        },
                      },
                      // Left: profile info
                      React.createElement(
                        "div",
                        { style: { flex: 1, minWidth: 0 } },
                        React.createElement(
                          Space,
                          { align: "center" },
                          React.createElement(
                            Text,
                            { strong: true, style: { fontSize: 14 } },
                            profile.name ||
                              `${profile.username}@${profile.host}`,
                          ),
                          connected
                            ? React.createElement(
                                Tag,
                                { color: "success" },
                                zh ? "已连接" : "Connected",
                              )
                            : profile.id === pendingProfileId
                              ? React.createElement(
                                  Tag,
                                  { color: "processing" },
                                  zh ? "待连接" : "Pending",
                                )
                              : null,
                        ),
                        React.createElement(
                          "div",
                          { style: { marginTop: 4 } },
                          React.createElement(
                            Text,
                            { type: "secondary", style: { fontSize: 12 } },
                            `${profile.username}@${profile.host}:${profile.port}`,
                            profile.key_path
                              ? `  |  ${zh ? "密钥" : "Key"}: ${profile.key_path}`
                              : "",
                            profile.jump_host_name
                              ? `  |  ${zh ? "经由" : "via"} ${profile.jump_host_name}`
                              : "",
                          ),
                        ),
                        connected
                          ? React.createElement(
                              "div",
                              { style: { marginTop: 4, display: "flex", alignItems: "center", gap: 6 } },
                              React.createElement(
                                Text,
                                { type: "secondary", style: { fontSize: 12 } },
                                `cwd: ${cwdValue}`,
                              ),
                              React.createElement(Button, {
                                type: "text", size: "small", icon: renderIcon(EditOutlined),
                                onClick: () => {
                                  const newCwd = prompt(zh ? "设置工作目录:" : "Set working directory:", cwdValue);
                                  if (newCwd && newCwd.trim()) {
                                    handleSetCwd(newCwd.trim());
                                  }
                                },
                              }),
                            )
                          : null,
                      ),
                      // Right: actions
                      React.createElement(
                        Space,
                        null,
                        isConnecting
                          ? React.createElement(LoadingOutlined, {
                              style: { fontSize: 18 },
                            })
                          : React.createElement(
                              Tooltip,
                              {
                                title: connected
                                  ? (zh ? "断开连接" : "Disconnect")
                                  : (zh ? "连接到此设备" : "Connect to this device"),
                              },
                              React.createElement(Switch, {
                                checked: connected,
                                onChange: () => handleToggleConnect(profile),
                                checkedChildren: zh ? "开" : "ON",
                                unCheckedChildren: zh ? "关" : "OFF",
                              }),
                            ),
                        React.createElement(
                          Tooltip,
                          { title: zh ? "编辑此连接配置" : "Edit this connection profile" },
                          React.createElement(Button, {
                            type: "text",
                            size: "small",
                            icon: renderIcon(EditOutlined),
                            onClick: () => openEditProfileModal(profile),
                          }),
                        ),
                        React.createElement(
                          Popconfirm,
                          {
                            title: zh ? "删除此连接配置？" : "Delete this connection profile?",
                            onConfirm: () => handleDelete(profile.id),
                            okText: zh ? "删除" : "Delete",
                            cancelText: zh ? "取消" : "Cancel",
                            okButtonProps: { danger: true },
                          },
                          React.createElement(Button, {
                            type: "text",
                            danger: true,
                            size: "small",
                            icon: renderIcon(DeleteOutlined),
                          }),
                        ),
                      ),
                    ),
                  );
                },
              },
            ),
      // New Connection Modal
      React.createElement(
        Modal,
        {
          title: editingProfile
            ? (zh ? "编辑 SSH 连接" : "Edit SSH Connection")
            : (zh ? "新建 SSH 连接" : "New SSH Connection"),
          open: modalOpen,
          onCancel: () => {
            setModalOpen(false);
            setEditingProfile(null);
            form.resetFields();
          },
          footer: null,
        },
        React.createElement(
          Form,
          { form, layout: "vertical", onFinish: handleSave },
          React.createElement(
            Form.Item,
            { name: "name", label: zh ? "显示名称" : "Display Name" },
            React.createElement(Input, {
              placeholder: zh ? "我的服务器（可选，留空自动生成）" : "My Server (optional, auto-generated if empty)",
            }),
          ),
          React.createElement(
            Form.Item,
            {
              name: "host",
              label: zh ? "主机" : "Host",
              rules: [{ required: true, message: zh ? "请输入主机地址" : "Please enter the host" }],
            },
            React.createElement(Input, {
              placeholder: "192.168.1.100 or example.com",
            }),
          ),
          React.createElement(
            Space,
            { style: { width: "100%" } },
            React.createElement(
              Form.Item,
              {
                name: "port",
                label: zh ? "端口" : "Port",
                initialValue: 22,
                style: { width: 120 },
              },
              React.createElement(Input, { type: "number" }),
            ),
            React.createElement(
              Form.Item,
              {
                name: "username",
                label: zh ? "用户名" : "Username",
                initialValue: "root",
                style: { flex: 1 },
              },
              React.createElement(Input),
            ),
          ),
          React.createElement(
            Form.Item,
            {
              name: "password",
              label: React.createElement(Space, null,
                zh ? "密码" : "Password",
                editingProfile?.has_password
                  ? React.createElement(Tag, { color: "green", style: { marginLeft: 4 } }, zh ? "已设置" : "Set")
                  : null,
              ),
            },
            React.createElement(Input.Password, {
              placeholder: editingProfile
                ? (zh ? "留空则保留已保存的密码" : "Leave empty to keep the saved password")
                : (zh ? "使用密钥认证时可留空" : "Leave empty if using key auth"),
            }),
          ),
          React.createElement(
            Form.Item,
            { name: "key_path", label: zh ? "SSH 密钥路径" : "SSH Key Path" },
            React.createElement(Input, {
              placeholder: editingProfile?.has_passphrase
                ? (zh ? "留空则保留已保存的密钥口令" : "Leave empty to keep saved passphrase")
                : (zh ? "/home/user/.ssh/id_rsa（可选）" : "/home/user/.ssh/id_rsa (optional)"),
            }),
          ),
          React.createElement(
            Form.Item,
            {
              name: "passphrase",
              label: React.createElement(Space, null,
                zh ? "密钥口令" : "Key Passphrase",
                editingProfile?.has_passphrase
                  ? React.createElement(Tag, { color: "green", style: { marginLeft: 4 } }, zh ? "已设置" : "Set")
                  : null,
              ),
            },
            React.createElement(Input.Password, {
              placeholder: editingProfile
                ? (zh ? "留空则保留已保存的密钥口令" : "Leave empty to keep the saved passphrase")
                : (zh ? "密钥加密时填写" : "If key is encrypted"),
            }),
          ),
          React.createElement(
            Form.Item,
            {
              name: "sudo_password",
              label: React.createElement(Space, null,
                zh ? "sudo 密码" : "Sudo Password",
                editingProfile?.has_sudo_password
                  ? React.createElement(Tag, { color: "green", style: { marginLeft: 4 } }, zh ? "已设置" : "Set")
                  : null,
              ),
              tooltip: zh
                ? "仅用于 sudo，不会复用 SSH 登录密码"
                : "Used only for sudo. Never reuses the SSH login password.",
            },
            React.createElement(Input.Password, {
              placeholder: editingProfile
                ? (zh ? "留空则保留已保存的 sudo 密码" : "Leave empty to keep the saved sudo password")
                : (zh ? "可选，非交互式 sudo 时填写" : "Optional; needed for non-interactive sudo"),
            }),
          ),
          React.createElement(
            Form.Item,
            {
              name: "jump_host_id",
              label: zh ? "跳板机" : "Jump Host",
            },
            React.createElement(Select, {
              allowClear: true,
              placeholder: zh ? "直连（不使用跳板机）" : "Direct connection (no jump host)",
              options: jumpHosts.map((jumpHost: any) => ({
                label:
                  jumpHost.name ||
                  `${jumpHost.username}@${jumpHost.host}:${jumpHost.port}`,
                value: jumpHost.id,
              })),
            }),
          ),
          React.createElement(
            Form.Item,
            {
              name: "accept_new_host_key",
              label: zh ? "信任未知主机密钥" : "Trust Unknown Host Key",
              valuePropName: "checked",
              initialValue: false,
              tooltip: zh
                ? "默认拒绝未记录在 known_hosts 中的主机密钥（防中间人攻击）。仅在核对过主机指纹后开启。"
                : "Unknown host keys are rejected by default to prevent man-in-the-middle attacks. Enable only after verifying the host fingerprint.",
            },
            React.createElement(Switch),
          ),
          React.createElement(
            Form.Item,
            { name: "default_cwd", label: zh ? "默认工作目录" : "Default Working Directory" },
            React.createElement(Input, {
              placeholder: zh ? "/workspace/app（可选，默认 /）" : "/workspace/app (optional, default: /)",
            }),
          ),
          React.createElement(
            Form.Item,
            null,
            React.createElement(
              Space,
              { wrap: true, style: { width: "100%" } },
              React.createElement(
                Button,
                {
                  htmlType: "button",
                  onClick: async () => {
                    const values = form.getFieldsValue();
                    if (!values.host) {
                      antdMessage.error(zh ? "请填写主机地址" : "Host is required for testing");
                      return;
                    }
                    try {
                      const useSavedProfile = editingProfile && !values.password && !values.passphrase;
                      const result = useSavedProfile
                        ? await apiFetch(`/remote/profiles/${editingProfile.id}/test`, { method: "POST" })
                        : await apiFetch("/remote/profiles/test", { method: "POST", body: JSON.stringify(values) });
                      if (result.ok) {
                        antdMessage.success(
                          zh
                            ? `连接成功 \u00B7 ${result.remote_os} \u00B7 ${result.remote_shell} \u00B7 ${result.latency_ms} ms`
                            : `Connection OK \u00B7 ${result.remote_os} \u00B7 ${result.remote_shell} \u00B7 ${result.latency_ms} ms`
                        );
                      } else {
                        antdMessage.error(zh ? `测试失败: ${result.error}` : `Test failed: ${result.error}`);
                      }
                    } catch (e: any) {
                      antdMessage.error(zh ? `测试失败: ${e.message}` : `Test failed: ${e.message}`);
                    }
                  },
                  style: { flex: 1, minWidth: 140 },
                },
                zh ? "测试连接" : "Test Connection",
              ),
              React.createElement(
                Button,
                {
                  type: "primary",
                  htmlType: "submit",
                  loading: saving,
                  style: { flex: 1, minWidth: 140 },
                },
                editingProfile ? (zh ? "更新配置" : "Update Profile") : (zh ? "保存配置" : "Save Profile"),
              ),
            ),
          ),
        ),
      ),
      React.createElement(
        Modal,
        {
          title: editingJumpHost
            ? (zh ? "编辑跳板机" : "Edit Jump Host")
            : (zh ? "新建跳板机" : "New Jump Host"),
          open: jumpModalOpen,
          onCancel: () => {
            setJumpModalOpen(false);
            setEditingJumpHost(null);
            jumpForm.resetFields();
          },
          footer: null,
        },
        React.createElement(
          Form,
          { form: jumpForm, layout: "vertical", onFinish: handleSaveJumpHost },
          React.createElement(
            Form.Item,
            { name: "name", label: zh ? "显示名称" : "Display Name" },
            React.createElement(Input, {
              placeholder: zh ? "跳板机（可选，留空自动生成）" : "Bastion (optional, auto-generated if empty)",
            }),
          ),
          React.createElement(
            Form.Item,
            {
              name: "host",
              label: zh ? "主机" : "Host",
              rules: [{ required: true, message: zh ? "请输入主机地址" : "Please enter the host" }],
            },
            React.createElement(Input, {
              placeholder: "bastion.example.com or 192.168.1.10",
            }),
          ),
          React.createElement(
            Space,
            { style: { width: "100%" } },
            React.createElement(
              Form.Item,
              {
                name: "port",
                label: zh ? "端口" : "Port",
                initialValue: 22,
                style: { width: 120 },
              },
              React.createElement(Input, { type: "number" }),
            ),
            React.createElement(
              Form.Item,
              {
                name: "username",
                label: zh ? "用户名" : "Username",
                initialValue: "root",
                style: { flex: 1 },
              },
              React.createElement(Input),
            ),
          ),
          React.createElement(
            Form.Item,
            {
              name: "password",
              label: React.createElement(Space, null,
                zh ? "密码" : "Password",
                editingJumpHost?.has_password
                  ? React.createElement(Tag, { color: "green", style: { marginLeft: 4 } }, zh ? "已设置" : "Set")
                  : null,
              ),
            },
            React.createElement(Input.Password, {
              placeholder: editingJumpHost
                ? (zh ? "留空则保留已保存的密码" : "Leave empty to keep the saved password")
                : (zh ? "使用密钥认证时可留空" : "Leave empty if using key auth"),
            }),
          ),
          React.createElement(
            Form.Item,
            { name: "key_path", label: zh ? "SSH 密钥路径" : "SSH Key Path" },
            React.createElement(Input, {
              placeholder: editingJumpHost?.has_passphrase
                ? (zh ? "留空则保留已保存的密钥口令" : "Leave empty to keep saved passphrase")
                : (zh ? "/home/user/.ssh/id_rsa（可选）" : "/home/user/.ssh/id_rsa (optional)"),
            }),
          ),
          React.createElement(
            Form.Item,
            {
              name: "passphrase",
              label: React.createElement(Space, null,
                zh ? "密钥口令" : "Key Passphrase",
                editingJumpHost?.has_passphrase
                  ? React.createElement(Tag, { color: "green", style: { marginLeft: 4 } }, zh ? "已设置" : "Set")
                  : null,
              ),
            },
            React.createElement(Input.Password, {
              placeholder: editingJumpHost
                ? (zh ? "留空则保留已保存的密钥口令" : "Leave empty to keep the saved passphrase")
                : (zh ? "密钥加密时填写" : "If key is encrypted"),
            }),
          ),
          React.createElement(
            Form.Item,
            null,
            React.createElement(
              Button,
              {
                type: "primary",
                htmlType: "submit",
                loading: savingJumpHost,
                style: { width: "100%" },
              },
              editingJumpHost
                ? (zh ? "更新跳板机" : "Update Jump Host")
                : (zh ? "保存跳板机" : "Save Jump Host"),
            ),
          ),
        ),
      ),
    );
  }

  // ── Header SSH Status Indicator ──────────────────────────────────────

  function RemoteStatusIndicator() {
    const zh = useZh();
    const [connection, setConnection] = useState(null as any);
    const [profiles, setProfiles] = useState([] as any[]);
    const [activeProfileId, setActiveProfileId] = useState("");
    const [connectingId, setConnectingId] = useState(null as string | null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");
    const [health, setHealth] = useState(null as any);
    const [remoteInfo, setRemoteInfo] = useState(null as any);
    const [reconnecting, setReconnecting] = useState(false);
    const prevConnectedRef = React.useRef(false);

    const [sessionId, setSessionId] = useState(getSessionId() || "");

    const fetchStatus = useCallback(async () => {
      try {
        setLoading(true);
        setError("");
        // The owner-scoped lookup works without a "current session", which the
        // host does not provide on non-chat routes.
        const [active, profileData] = await Promise.all([
          fetchActiveConnection(),
          apiFetch("/remote/profiles"),
        ]);
        const newConn = active?.connection || null;
        const sid = active?.session_id
          ? String(active.session_id)
          : getSessionId() || "";
        setSessionId(sid);

        let nextRemoteInfo = null as any;
        if (newConn && sid) {
          const infoData = await apiFetch(
            `/remote/connections/${sid}/info`,
          ).catch(() => ({ info: null }));
          nextRemoteInfo = infoData.info || null;
        }
        const wasConnected = prevConnectedRef.current;
        const isNowConnected = newConn !== null;
        if (wasConnected && !isNowConnected) {
          antdMessage.warning(zh ? "SSH 连接已断开" : "SSH connection lost");
        }
        prevConnectedRef.current = isNowConnected;
        setConnection(newConn);
        setProfiles(profileData.profiles || []);
        setActiveProfileId(newConn?.profile_id || "");
        setHealth(active?.health || null);
        setRemoteInfo(nextRemoteInfo);
      } catch (e: any) {
        const errorMsg = e.message || String(e);
        console.error("[Remote] Failed to fetch status:", e);
        if (prevConnectedRef.current) {
          antdMessage.warning(zh ? "SSH 连接已断开" : "SSH connection lost");
        }
        prevConnectedRef.current = false;
        setError(errorMsg);
        setConnection(null);
        setHealth(null);
        setRemoteInfo(null);
      } finally {
        setLoading(false);
      }
    }, [zh]);

    useEffect(() => {
      fetchStatus();
      const interval = setInterval(fetchStatus, 5000);
      return () => clearInterval(interval);
    }, [fetchStatus]);

    const handleDisconnect = async () => {
      if (!requireSessionId(zh, sessionId)) return;
      try {
        await apiFetch(`/remote/connections/${sessionId}`, {
          method: "DELETE",
        });
        fetchStatus();
      } catch (e: any) {
        antdMessage.error(zh ? `断开连接失败: ${e.message}` : `Disconnect failed: ${e.message}`);
      }
    };

    const handleReconnect = async () => {
      if (!requireSessionId(zh, sessionId)) return;
      setReconnecting(true);
      try {
        await apiFetch(`/remote/connections/${sessionId}/reconnect`, {
          method: "POST",
        });
        antdMessage.success(zh ? "已重连" : "Reconnected");
        fetchStatus();
      } catch (e: any) {
        antdMessage.error(zh ? `重连失败: ${e.message}` : `Reconnect failed: ${e.message}`);
      } finally {
        setReconnecting(false);
      }
    };

    const handleProfileClick = async (profile: any) => {
      const activeSessionId = requireSessionId(zh, sessionId);
      if (!activeSessionId) return;

      const isActive = profile.id === activeProfileId;
      setConnectingId(profile.id);
      try {
        if (isActive) {
          await apiFetch(`/remote/connections/${activeSessionId}`, {
            method: "DELETE",
          });
        } else {
          const result = await connectViaProfile(
            profile.id,
            activeSessionId,
            zh,
          );
          antdMessage.success(zh ? `已连接到 ${profile.name}` : `Connected to ${profile.name}`);

          if (result.sudo_needs_password) {
            const sudoPwd = prompt(
              zh ? "未配置 sudo 密码，需要时请输入（留空跳过）：" : "Sudo password is not configured. Enter it if needed (empty to skip):"
            );
            if (sudoPwd) {
              await apiFetch(`/remote/connections/${activeSessionId}/sudo`, {
                method: "POST",
                body: JSON.stringify({ password: sudoPwd, enabled: true }),
              });
            }
          }
        }
        fetchStatus();
      } catch (e: any) {
        antdMessage.error(
          zh
            ? `${isActive ? "断开" : "连接"}失败: ${e.message}`
            : `${isActive ? "Disconnect" : "Connection"} failed: ${e.message}`,
        );
      } finally {
        setConnectingId(null);
      }
    };

    const isConnected = connection !== null;
    const uptime = connection?.uptime_seconds || 0;
    let uptimeStr = "";
    if (isConnected) {
      if (uptime < 60) uptimeStr = `${uptime.toFixed(0)}s`;
      else if (uptime < 3600) uptimeStr = `${(uptime / 60).toFixed(0)}m`;
      else uptimeStr = `${(uptime / 3600).toFixed(1)}h`;
    }
    const deviceOs = remoteInfo?.remote_os || connection?.remote_os || "";
    const deviceArch = remoteInfo?.remote_arch || connection?.remote_arch || "";
    const deviceShell = remoteInfo?.remote_shell || connection?.remote_shell || "";
    const deviceSummary = [deviceOs, deviceArch, deviceShell].filter(Boolean).join(" · ");
    const shortDeviceLabel = [deviceOs, deviceArch].filter(Boolean).join(" ");

    const trigger = React.createElement(
      "button",
      {
        id: "remote-ssh-header-status-react",
        type: "button",
        style: {
          height: 38,
          minWidth: 156,
          maxWidth: 220,
          padding: "0 12px",
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 8,
          border: `1px solid ${isConnected ? theme.successBorder : theme.border}`,
          borderRadius: 6,
          background: isConnected ? theme.successBg : theme.bgContainer,
          color: isConnected ? theme.success : theme.text,
          font: "inherit",
          cursor: "pointer",
          whiteSpace: "nowrap",
          overflow: "hidden",
        },
        "aria-label": isConnected
          ? `SSH connected to ${connection.username}@${connection.host}`
          : "SSH disconnected",
      },
      React.createElement(Badge, {
        status: isConnected ? "success" : error ? "error" : "default",
      }),
      React.createElement(
        "span",
        {
          style: {
            minWidth: 0,
            overflow: "hidden",
            textOverflow: "ellipsis",
            fontSize: 14,
            fontWeight: 600,
          },
        },
        isConnected
          ? `${connection.username}@${connection.host}`
          : loading
            ? "SSH Checking"
            : "SSH Offline",
      ),
      isConnected && shortDeviceLabel
        ? React.createElement(
            Tag,
            {
              color: "green",
              style: {
                marginInlineStart: 0,
                maxWidth: 92,
                overflow: "hidden",
                textOverflow: "ellipsis",
              },
            },
            shortDeviceLabel,
          )
        : null,
    );

    const content = React.createElement(
      "div",
      { style: { width: 320 } },
      React.createElement(
        Space,
        { direction: "vertical", size: 10, style: { width: "100%" } },
        isConnected
          ? React.createElement(
              Space,
              { direction: "vertical", size: 6, style: { width: "100%" } },
              React.createElement(
                Space,
                { align: "center" },
                React.createElement(LaptopOutlined || CloudOutlined || "span"),
                React.createElement(Text, { strong: true }, zh ? "SSH 已连接" : "SSH Connected"),
                health?.latency_ms != null
                  ? React.createElement(Tag, { color: "blue", style: { marginLeft: 4 } }, `${health.latency_ms.toFixed(0)} ms`)
                  : null,
              ),
              React.createElement(
                Text,
                { code: true, ellipsis: true, style: { maxWidth: 296 } },
                `${connection.username}@${connection.host}:${connection.port}`,
              ),
              React.createElement(
                Space,
                { size: 6 },
                React.createElement(ThunderboltOutlined || "span"),
                React.createElement(Text, { type: "secondary" }, uptimeStr),
              ),
              deviceSummary
                ? React.createElement(
                    "div",
                    { style: { fontSize: 12, color: "var(--ant-color-text-secondary, #888)" } },
                    deviceSummary,
                  )
                : null,
            )
          : React.createElement(
              Space,
              { direction: "vertical", size: 6, style: { width: "100%" } },
              React.createElement(
                Text,
                { type: error ? "danger" : "secondary", style: { fontSize: 12 } },
                error || (zh ? "当前会话无活跃 SSH 连接。" : "No active SSH connection for this chat."),
              ),
              health?.reconnect_available
                ? React.createElement(
                    Button,
                    { type: "primary", size: "small", loading: reconnecting, onClick: handleReconnect },
                    zh ? "重新连接" : "Reconnect",
                  )
                : null,
            ),
        React.createElement(
          "div",
          { style: { borderTop: `1px solid ${theme.border}`, paddingTop: 8 } },
          React.createElement(Text, { strong: true }, zh ? "已保存设备" : "Saved Devices"),
        ),
        profiles.length === 0
          ? React.createElement(
              Text,
              { type: "secondary", style: { fontSize: 12 } },
              "No saved devices. Add one from Remote SSH.",
            )
          : React.createElement(
              Space,
              { direction: "vertical", size: 6, style: { width: "100%" } },
              ...profiles.map((profile: any) => {
                const active = profile.id === activeProfileId;
                return React.createElement(
                  "div",
                  {
                    key: profile.id,
                    style: {
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: 8,
                    },
                  },
                  React.createElement(
                    "div",
                    { style: { flex: 1, minWidth: 0, overflow: "hidden" } },
                    React.createElement(
                      "div",
                      {
                        title:
                          profile.name ||
                          `${profile.username}@${profile.host}`,
                        style: {
                          color: theme.text,
                          fontSize: 14,
                          fontWeight: active ? 600 : 500,
                          lineHeight: "20px",
                          maxWidth: 200,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        },
                      },
                      profile.name || `${profile.username}@${profile.host}`,
                    ),
                    React.createElement(
                      "div",
                      {
                        title: `${profile.username}@${profile.host}:${profile.port}`,
                        style: {
                          color: theme.secondaryText,
                          fontSize: 12,
                          lineHeight: "18px",
                          maxWidth: 200,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        },
                      },
                      `${profile.username}@${profile.host}:${profile.port}`,
                    ),
                    profile.jump_host_name
                      ? React.createElement(
                          "div",
                          {
                            title: `via ${profile.jump_host_name}`,
                            style: {
                              color: theme.secondaryText,
                              fontSize: 12,
                              lineHeight: "18px",
                              maxWidth: 200,
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                            },
                          },
                          `via ${profile.jump_host_name}`,
                        )
                      : null,
                  ),
                  React.createElement(
                    Button,
                    {
                      size: "small",
                      type: active ? "default" : "primary",
                      danger: active,
                      loading: connectingId === profile.id,
                      onClick: () => handleProfileClick(profile),
                    },
                    active ? "Disconnect" : "Connect",
                  ),
                );
              }),
            ),
      ),
    );

    return React.createElement(
      Popover,
      {
        content,
        trigger: "click",
        placement: "bottom",
      },
      React.createElement(Tooltip, {
        title: isConnected
          ? `${connection.username}@${connection.host}:${connection.port}`
          : "No active SSH connection",
      }, trigger),
    );
  }

  function registerHeaderStatus() {
    qwenpaw.slot.fill(
      REMOTE_PLUGIN_ID,
      "header.left",
      () => React.createElement(RemoteStatusIndicator),
      { id: "remote-ssh-status", order: 15 },
    );
  }

  // ── Register plugin ──────────────────────────────────────────────────

  const TOOL_RENDERERS: Record<string, (props: { data: any }) => any> = {
    remote_connect: RemoteConnectRender,
    remote_disconnect: RemoteDisconnectRender,
    remote_list: RemoteListRender,
    remote_exec: RemoteExecRender,
    remote_reconnect: RemoteReconnectRender,
    remote_info: RemoteInfoRender,
    remote_health: RemoteHealthRender,
    remote_set_cwd: RemoteSetCwdRender,
    remote_sudo: RemoteSudoRender,
  };

  for (const [toolName, renderer] of Object.entries(TOOL_RENDERERS)) {
    // chat.toolRender passes an untyped props record; the renderers normalize
    // it, so pass the whole object rather than assuming a `result` key. The
    // collapsible wrapper is applied once here for every tool card.
    qwenpaw.chat.toolRender(
      REMOTE_PLUGIN_ID,
      toolName,
      (props: Record<string, unknown>) => {
        logToolShape(props, toolName, parseToolOutput(props));
        return React.createElement(
          CollapsibleToolBody,
          null,
          React.createElement(renderer, { data: props }),
        );
      },
    );
  }

  const REMOTE_ROUTE_ID = "remote.main";

  // The host passes the resolved session id to chat request transforms. Cache
  // it so session-scoped calls work on hosts that expose neither
  // getCurrentSessionId() nor a session global.
  qwenpaw.chat.requestPayload.add(
    REMOTE_PLUGIN_ID,
    ({ sessionId }: { sessionId?: string }) => {
      if (sessionId) cachedSessionId = String(sessionId);
      // Returning undefined leaves the outgoing request body untouched.
      return undefined;
    },
    { id: "remote.session-capture", order: 100 },
  );

  qwenpaw.route.add(REMOTE_PLUGIN_ID, {
    id: REMOTE_ROUTE_ID,
    path: "/remote",
    component: RemotePage,
  });

  qwenpaw.menu.add(REMOTE_PLUGIN_ID, {
    id: REMOTE_ROUTE_ID,
    label: "Remote SSH",
    icon: "\u{1F517}",
    route: REMOTE_ROUTE_ID,
    location: "primary.settings",
    order: 20,
  });

  registerHeaderStatus();
}

// Auto-initialize when loaded. The host mounts window.QwenPaw (Host SDK and
// registration API) before it downloads plugin bundles.
function isQwenPawHostReady() {
  const host = (window as any).QwenPaw?.host;
  return Boolean(host?.React && host?.antd && host?.getApiUrl);
}

function initializeWhenReady() {
  if (!isQwenPawHostReady()) {
    console.error(
      "[Remote] window.QwenPaw Host SDK is unavailable; the Remote SSH UI " +
        "was not registered.",
    );
    return;
  }
  buildPlugin();
}

initializeWhenReady();
