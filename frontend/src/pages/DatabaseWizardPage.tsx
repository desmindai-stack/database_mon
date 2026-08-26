import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api,
  Application,
  ConnectionTestResult,
  DatabaseGroup,
  DbEngine,
  ENGINE_DEFAULTS,
  GroupEnvironment,
  GroupTopology,
  NodeRoleHint,
  NodeSite,
  ServerOS,
  WizardClusterOptions,
  WizardCreateGroupRequest,
  WizardNodeInput,
} from "../api";

type TopologyPreset = "standalone" | "cluster-2" | "cluster-3" | "cluster-custom";
type WizardMode = "create-group" | "add-node";

const TOPOLOGY_CARDS: { preset: TopologyPreset; title: string; description: string }[] = [
  { preset: "standalone", title: "Standalone", description: "Tek sunucu, tek instance." },
  { preset: "cluster-2", title: "Cluster — 2 düğüm", description: "Aynı veri merkezinde 2 düğüm." },
  { preset: "cluster-3", title: "Cluster — 3 düğüm", description: "2 düğüm ana DC + 1 düğüm disaster site." },
  { preset: "cluster-custom", title: "Cluster — özel", description: "2-8 arası düğüm, her biri için site seçilebilir." },
];

let nodeKeySeq = 0;
function nextNodeKey(): string {
  nodeKeySeq += 1;
  return `node-${nodeKeySeq}`;
}

type SqlServerAuthType = "sql" | "windows";
type PostgresSslMode = "disable" | "require";

interface NodeFormState {
  key: string;
  server_name: string;
  host: string;
  ip_address: string;
  os: ServerOS;
  site: NodeSite;
  agent_url: string;
  agent_token: string;
  instance_name: string;
  port: number;
  database: string;
  db_username: string;
  db_password: string;
  role_hint: NodeRoleHint;
  // postgresql only.
  sslMode: PostgresSslMode;
  // sqlserver only.
  authType: SqlServerAuthType;
  // mongodb only.
  replicaSet: string;
  authSource: string;
  testing: boolean;
  testResult: ConnectionTestResult | null;
  agentTesting: boolean;
  agentTestResult: ConnectionTestResult | null;
}

function makeNode(engine: DbEngine, site: NodeSite, role_hint: NodeRoleHint): NodeFormState {
  return {
    key: nextNodeKey(),
    server_name: "",
    host: "",
    ip_address: "",
    os: engine === "sqlserver" ? "windows" : "linux",
    site,
    agent_url: "",
    agent_token: "",
    instance_name: "",
    port: ENGINE_DEFAULTS[engine].port,
    database: ENGINE_DEFAULTS[engine].database,
    db_username: engine === "sqlserver" ? "sa" : engine === "mongodb" ? "admin" : "postgres",
    db_password: "",
    role_hint,
    sslMode: "disable",
    authType: "sql",
    replicaSet: "",
    authSource: engine === "mongodb" ? ENGINE_DEFAULTS[engine].database : "",
    testing: false,
    testResult: null,
    agentTesting: false,
    agentTestResult: null,
  };
}

function nodesForPreset(preset: TopologyPreset, engine: DbEngine): NodeFormState[] {
  switch (preset) {
    case "standalone":
      return [makeNode(engine, "primary", "unknown")];
    case "cluster-2":
      return [makeNode(engine, "primary", "primary"), makeNode(engine, "primary", "replica")];
    case "cluster-3":
      return [
        makeNode(engine, "primary", "primary"),
        makeNode(engine, "primary", "replica"),
        makeNode(engine, "disaster", "replica"),
      ];
    case "cluster-custom":
      return [makeNode(engine, "primary", "primary"), makeNode(engine, "primary", "replica")];
  }
}

function topologyFor(preset: TopologyPreset, engine: DbEngine): GroupTopology {
  // MongoDB has no cluster/replica-set topology modeled in dbace (GroupTopology is
  // {standalone, patroni, alwayson}) — only standalone is meaningful, enforced both here and
  // by hiding the cluster topology cards below.
  if (preset === "standalone" || engine === "mongodb") return "standalone";
  return engine === "sqlserver" ? "alwayson" : "patroni";
}

const REQUIRED = <span className="required-mark">*</span>;

function nodeToWizardInput(n: NodeFormState, engine: DbEngine): WizardNodeInput {
  return {
    server_name: n.server_name.trim(),
    host: n.host.trim(),
    ip_address: n.ip_address.trim() || null,
    os: n.os,
    site: n.site,
    agent_url: n.agent_url.trim() || null,
    agent_token: n.agent_token.trim() || null,
    instance_name: engine === "sqlserver" ? n.instance_name.trim() || null : null,
    port: n.port,
    database: n.database.trim() || null,
    db_username: n.db_username.trim(),
    db_password: n.db_password,
    role_hint: n.role_hint,
    ssl_mode: engine === "postgresql" ? n.sslMode : null,
    auth_type: engine === "sqlserver" ? n.authType : null,
    replica_set: engine === "mongodb" ? n.replicaSet.trim() || null : null,
    auth_source: engine === "mongodb" ? n.authSource.trim() || null : null,
  };
}

export default function DatabaseWizardPage() {
  const { applicationId, groupId } = useParams<{ applicationId?: string; groupId?: string }>();
  const appId = Number(applicationId);
  const gId = Number(groupId);
  const mode: WizardMode = groupId ? "add-node" : "create-group";
  const navigate = useNavigate();

  const [application, setApplication] = useState<Application | null>(null);
  const [existingGroup, setExistingGroup] = useState<DatabaseGroup | null>(null);
  const [existingNodeCount, setExistingNodeCount] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [step, setStep] = useState(0);
  const [engine, setEngine] = useState<DbEngine>("postgresql");
  const [preset, setPreset] = useState<TopologyPreset>("standalone");
  const [customNodeCount, setCustomNodeCount] = useState(2);

  const [groupName, setGroupName] = useState("");
  const [clusterName, setClusterName] = useState("");
  const [accessName, setAccessName] = useState("");
  const [vipAddress, setVipAddress] = useState("");
  const [listenerPort, setListenerPort] = useState<number | "">("");
  const [environment, setEnvironment] = useState<GroupEnvironment>("prod");
  const [notes, setNotes] = useState("");

  const [patroniPort, setPatroniPort] = useState<number | "">(8008);
  const [etcdPort, setEtcdPort] = useState<number | "">(2379);
  const [haproxyStatsPort, setHaproxyStatsPort] = useState<number | "">(8404);
  const [keepalivedVip, setKeepalivedVip] = useState("");

  const [nodes, setNodes] = useState<NodeFormState[]>(() =>
    mode === "add-node" ? [makeNode("postgresql", "primary", "replica")] : nodesForPreset("standalone", "postgresql")
  );
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // In add-node mode, topology/engine are fixed by the group being added to; the topology
  // preset system (used only for the create-group flow's Step 1) is bypassed entirely.
  const topology: GroupTopology =
    mode === "add-node" && existingGroup ? (existingGroup.topology as GroupTopology) : topologyFor(preset, engine);
  const isCluster = topology !== "standalone";
  const minNodesInStep = mode === "add-node" ? 1 : 2;
  const maxNewNodes = mode === "add-node" ? Math.max(1, 8 - existingNodeCount) : 8;

  useEffect(() => {
    if (mode === "create-group") {
      if (!appId) return;
      api.getApplication(appId).then(setApplication).catch(() => undefined);
      return;
    }
    if (!gId) return;
    Promise.all([api.getGroup(gId), api.getGroupNodes(gId)])
      .then(([group, groupNodes]) => {
        setExistingGroup(group);
        setExistingNodeCount(groupNodes.length);
        setEngine(group.engine);
        setNodes([makeNode(group.engine, "primary", "replica")]);
      })
      .catch((e) => setLoadError(String((e as Error).message || e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, appId, gId]);

  const steps = useMemo(() => {
    if (mode === "add-node") {
      return [
        { key: "nodes", label: "Yeni düğümler" },
        { key: "summary", label: "Özet ve onay" },
      ];
    }
    const list = [{ key: "topology", label: "Topoloji" }];
    if (isCluster) list.push({ key: "cluster", label: "Cluster bilgileri" });
    list.push({ key: "nodes", label: isCluster ? "Düğümler" : "Sunucu ve instance" });
    list.push({ key: "summary", label: "Özet ve onay" });
    return list;
  }, [isCluster, mode]);

  const onSelectTopology = (nextPreset: TopologyPreset) => {
    setPreset(nextPreset);
    const count = nextPreset === "cluster-custom" ? customNodeCount : undefined;
    let next = nodesForPreset(nextPreset, engine);
    if (nextPreset === "cluster-custom" && count) {
      next = Array.from({ length: count }, (_, i) =>
        i === 0 ? makeNode(engine, "primary", "primary") : makeNode(engine, "primary", "replica")
      );
    }
    setNodes(next);
    setFieldErrors({});
  };

  const onSelectEngine = (nextEngine: DbEngine) => {
    setEngine(nextEngine);
    if (nextEngine === "mongodb" && preset !== "standalone") {
      setPreset("standalone");
      setNodes(nodesForPreset("standalone", nextEngine));
      return;
    }
    setNodes((prev) =>
      prev.map((n) => ({
        ...n,
        os: nextEngine === "sqlserver" ? "windows" : "linux",
        port: ENGINE_DEFAULTS[nextEngine].port,
        database: ENGINE_DEFAULTS[nextEngine].database,
        db_username: nextEngine === "sqlserver" ? "sa" : nextEngine === "mongodb" ? "admin" : "postgres",
        authSource: nextEngine === "mongodb" ? ENGINE_DEFAULTS[nextEngine].database : n.authSource,
      }))
    );
  };

  const onCustomNodeCountChange = (count: number) => {
    const clamped = Math.max(2, Math.min(8, count));
    setCustomNodeCount(clamped);
    setNodes((prev) => {
      if (clamped > prev.length) {
        const additions = Array.from({ length: clamped - prev.length }, () => makeNode(engine, "primary", "replica"));
        return [...prev, ...additions];
      }
      return prev.slice(0, clamped);
    });
  };

  const canAddNode = mode === "add-node" || (isCluster && preset === "cluster-custom");

  const addNode = () => {
    if (nodes.length >= maxNewNodes) return;
    setNodes((prev) => [...prev, makeNode(engine, "primary", "replica")]);
  };

  const removeNode = (key: string) => {
    setNodes((prev) => (prev.length <= minNodesInStep ? prev : prev.filter((n) => n.key !== key)));
  };

  const updateNode = <K extends keyof NodeFormState>(key: string, field: K, value: NodeFormState[K]) => {
    setNodes((prev) => prev.map((n) => (n.key === key ? { ...n, [field]: value } : n)));
  };

  const defaultDatabase = () => ENGINE_DEFAULTS[engine].database;

  const testNodeConnection = async (key: string) => {
    const node = nodes.find((n) => n.key === key);
    if (!node) return;
    updateNode(key, "testing", true);
    updateNode(key, "testResult", null);
    try {
      const options =
        engine === "postgresql"
          ? { ssl_mode: node.sslMode }
          : engine === "sqlserver"
            ? { auth_type: node.authType }
            : engine === "mongodb"
              ? { authSource: node.authSource || undefined, replica_set: node.replicaSet || undefined }
              : undefined;
      const result = await api.testConnection({
        name: `${groupName || existingGroup?.name || "wizard"}-${node.server_name || "node"}-test`,
        engine,
        host: node.host,
        port: node.port,
        database: node.database || defaultDatabase(),
        username: node.db_username,
        password: node.db_password,
        options,
      });
      updateNode(key, "testResult", result);
    } catch (err) {
      updateNode(key, "testResult", { ok: false, message: String((err as Error).message), details: {} });
    } finally {
      updateNode(key, "testing", false);
    }
  };

  const testNodeAgent = async (key: string) => {
    const node = nodes.find((n) => n.key === key);
    if (!node) return;
    if (!node.agent_url) {
      updateNode(key, "agentTestResult", { ok: false, message: "Önce agent URL girin", details: {} });
      return;
    }
    updateNode(key, "agentTesting", true);
    updateNode(key, "agentTestResult", null);
    try {
      const result = await api.testServerAgent(node.agent_url, node.agent_token || undefined);
      updateNode(key, "agentTestResult", result);
    } catch (err) {
      updateNode(key, "agentTestResult", { ok: false, message: String((err as Error).message), details: {} });
    } finally {
      updateNode(key, "agentTesting", false);
    }
  };

  const testAllNodes = () => {
    nodes.forEach((n) => {
      testNodeConnection(n.key);
      if (n.agent_url) testNodeAgent(n.key);
    });
  };

  const validateClusterStep = (): Record<string, string> => {
    const errors: Record<string, string> = {};
    if (!groupName.trim()) errors.group_name = "Grup adı zorunlu";
    if (isCluster) {
      if (!clusterName.trim()) errors.cluster_name = "Cluster adı zorunlu";
      if (!accessName.trim()) errors.access_name = "Erişim adı (listener/VIP) zorunlu";
    }
    return errors;
  };

  const validateNodesStep = (): Record<string, string> => {
    const errors: Record<string, string> = {};
    const seenNames = new Set<string>();
    nodes.forEach((n, idx) => {
      if (!n.server_name.trim()) errors[`node-${idx}-server_name`] = "Sunucu adı zorunlu";
      else if (seenNames.has(n.server_name.trim())) errors[`node-${idx}-server_name`] = "Bu sunucu adı listede tekrar ediyor";
      seenNames.add(n.server_name.trim());
      if (!n.host.trim()) errors[`node-${idx}-host`] = "Hostname zorunlu";
      const usernameNeeded = !(engine === "sqlserver" && n.authType === "windows");
      if (usernameNeeded && !n.db_username.trim()) errors[`node-${idx}-db_username`] = "Kullanıcı adı zorunlu";
      if (!n.port || n.port <= 0) errors[`node-${idx}-port`] = "Geçerli bir port girin";
    });
    return errors;
  };

  const goNext = () => {
    const currentKey = steps[step].key;
    let errors: Record<string, string> = {};
    if (currentKey === "cluster") errors = validateClusterStep();
    if (currentKey === "nodes") {
      errors = mode === "add-node" ? validateNodesStep() : { ...(isCluster ? {} : validateClusterStep()), ...validateNodesStep() };
    }
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;
    setStep((s) => Math.min(s + 1, steps.length - 1));
  };

  const goBack = () => setStep((s) => Math.max(s - 1, 0));

  const buildCreateGroupPayload = (): WizardCreateGroupRequest => {
    const clusterOptions: WizardClusterOptions | null =
      topology === "patroni"
        ? {
            patroni_port: patroniPort === "" ? null : Number(patroniPort),
            etcd_port: etcdPort === "" ? null : Number(etcdPort),
            haproxy_stats_port: haproxyStatsPort === "" ? null : Number(haproxyStatsPort),
            keepalived_vip: keepalivedVip || null,
          }
        : null;

    return {
      application_id: appId,
      group_name: groupName.trim(),
      engine,
      topology,
      environment,
      access_name: isCluster ? accessName.trim() : undefined,
      cluster_name: isCluster ? clusterName.trim() : undefined,
      vip_address: isCluster ? vipAddress.trim() || undefined : undefined,
      listener_port: isCluster && listenerPort !== "" ? Number(listenerPort) : undefined,
      notes: notes.trim() || undefined,
      cluster_options: clusterOptions,
      nodes: nodes.map((n) => nodeToWizardInput(n, engine)),
    };
  };

  const onSave = async () => {
    if (mode === "add-node") {
      const errors = validateNodesStep();
      setFieldErrors(errors);
      if (Object.keys(errors).length > 0) {
        setSaveError("Eksik alanlar var — önceki adıma dönüp tamamlayın.");
        return;
      }
      setSaving(true);
      setSaveError(null);
      try {
        await api.addNodesWizard(gId, { nodes: nodes.map((n) => nodeToWizardInput(n, engine)) });
        navigate(`/groups/${gId}`);
      } catch (err) {
        setSaveError(String((err as Error).message));
      } finally {
        setSaving(false);
      }
      return;
    }

    const errors = { ...validateClusterStep(), ...validateNodesStep() };
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) {
      setSaveError("Eksik alanlar var — önceki adımlara dönüp tamamlayın.");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const group = await api.createGroupWizard(buildCreateGroupPayload());
      navigate(`/groups/${group.id}`);
    } catch (err) {
      setSaveError(String((err as Error).message));
    } finally {
      setSaving(false);
    }
  };

  const currentKey = steps[step]?.key;

  if (mode === "add-node" && !existingGroup && !loadError) {
    return <div className="empty">Yükleniyor…</div>;
  }

  return (
    <>
      <header className="page-header">
        <div>
          <h2>{mode === "add-node" ? `Düğüm ekle — ${existingGroup?.name ?? ""}` : "Veritabanı ekle — sihirbaz"}</h2>
          <p>
            {mode === "add-node" && existingGroup && <Link to={`/groups/${existingGroup.id}`}>← {existingGroup.name}</Link>}
            {mode === "create-group" && application && (
              <Link to={`/applications/${application.id}/groups`}>← {application.name}</Link>
            )}
          </p>
        </div>
      </header>

      {loadError && <div className="error">{loadError}</div>}

      <div className="wizard-steps">
        {steps.map((s, idx) => (
          <div key={s.key} className={`wizard-step${idx === step ? " active" : ""}${idx < step ? " done" : ""}`}>
            <span className="wizard-step-num">{idx + 1}</span>
            {s.label}
          </div>
        ))}
      </div>

      {saveError && <div className="error">{saveError}</div>}

      <div className="card">
        {currentKey === "topology" && (
          <>
            <h3 className="chart-title">Motor ve topoloji seçin</h3>
            <div className="form-grid" style={{ maxWidth: 320, marginBottom: "1.25rem" }}>
              <label>
                Motor {REQUIRED}
                <select
                  value={engine}
                  onChange={(e) => onSelectEngine(e.target.value as DbEngine)}
                >
                  <option value="postgresql">PostgreSQL</option>
                  <option value="sqlserver">SQL Server</option>
                  <option value="mongodb">MongoDB</option>
                </select>
              </label>
            </div>
            <div className="wizard-topology-grid">
              {TOPOLOGY_CARDS.map((card) => {
                const disabled = engine === "mongodb" && card.preset !== "standalone";
                return (
                  <button
                    key={card.preset}
                    type="button"
                    disabled={disabled}
                    title={disabled ? "MongoDB için şu anda sadece standalone destekleniyor" : undefined}
                    className={`wizard-topology-card${preset === card.preset ? " selected" : ""}${disabled ? " disabled" : ""}`}
                    onClick={() => onSelectTopology(card.preset)}
                  >
                    <h4>{card.title}</h4>
                    <p>{card.description}</p>
                  </button>
                );
              })}
            </div>
            {preset === "cluster-custom" && (
              <label style={{ maxWidth: 220 }}>
                Düğüm sayısı (2-8)
                <input
                  type="number"
                  min={2}
                  max={8}
                  value={customNodeCount}
                  onChange={(e) => onCustomNodeCountChange(Number(e.target.value))}
                />
              </label>
            )}
            <p className="muted-note" style={{ marginTop: "0.75rem" }}>
              {preset === "standalone" && "Tek sunucu, tek instance oluşturulacak."}
              {preset === "cluster-2" && `2 düğümlü ${engine === "sqlserver" ? "Always On" : "Patroni"} cluster — aynı veri merkezi.`}
              {preset === "cluster-3" && `3 düğümlü ${engine === "sqlserver" ? "Always On" : "Patroni"} cluster — 2 ana DC + 1 disaster site.`}
              {preset === "cluster-custom" && `${nodes.length} düğümlü özel ${engine === "sqlserver" ? "Always On" : "Patroni"} cluster.`}
            </p>
            <div className="form-actions">
              <button type="button" className="btn btn-primary" onClick={goNext}>İleri</button>
            </div>
          </>
        )}

        {currentKey === "cluster" && (
          <>
            <h3 className="chart-title">Cluster bilgileri</h3>
            <div className="form-grid" style={{ maxWidth: 480 }}>
              <label>
                Grup adı {REQUIRED}
                <input
                  value={groupName}
                  onChange={(e) => setGroupName(e.target.value)}
                  className={fieldErrors.group_name ? "field-invalid" : ""}
                  placeholder="boa-sqlserver-ag"
                />
                {fieldErrors.group_name && <span className="field-error">{fieldErrors.group_name}</span>}
              </label>
              <label>
                Cluster adı {REQUIRED}
                <input
                  value={clusterName}
                  onChange={(e) => setClusterName(e.target.value)}
                  className={fieldErrors.cluster_name ? "field-invalid" : ""}
                  placeholder="boa-ag"
                />
                {fieldErrors.cluster_name && <span className="field-error">{fieldErrors.cluster_name}</span>}
              </label>
              <label>
                {engine === "sqlserver" ? "Listener adı" : "VIP / HAProxy adresi"} {REQUIRED}
                <input
                  value={accessName}
                  onChange={(e) => setAccessName(e.target.value)}
                  className={fieldErrors.access_name ? "field-invalid" : ""}
                  placeholder={engine === "sqlserver" ? "boa-ag-listener.internal" : "aapara-patroni-vip.internal"}
                />
                {fieldErrors.access_name && <span className="field-error">{fieldErrors.access_name}</span>}
              </label>
              <label>
                {engine === "sqlserver" ? "Listener IP" : "VIP adresi"}
                <input value={vipAddress} onChange={(e) => setVipAddress(e.target.value)} placeholder="10.0.0.50" />
              </label>
              <label>
                {engine === "sqlserver" ? "Listener port" : "VIP / HAProxy portu"}
                <input
                  type="number"
                  value={listenerPort}
                  onChange={(e) => setListenerPort(e.target.value === "" ? "" : Number(e.target.value))}
                  placeholder={engine === "sqlserver" ? "1433" : "5000"}
                />
              </label>
              <label>
                Ortam
                <select value={environment} onChange={(e) => setEnvironment(e.target.value as GroupEnvironment)}>
                  <option value="prod">Prod</option>
                  <option value="preprod">Preprod</option>
                  <option value="test">Test</option>
                  <option value="dev">Dev</option>
                </select>
              </label>
              {topology === "patroni" && (
                <>
                  <label>
                    Patroni REST portu
                    <input
                      type="number"
                      value={patroniPort}
                      onChange={(e) => setPatroniPort(e.target.value === "" ? "" : Number(e.target.value))}
                    />
                  </label>
                  <label>
                    etcd portu
                    <input
                      type="number"
                      value={etcdPort}
                      onChange={(e) => setEtcdPort(e.target.value === "" ? "" : Number(e.target.value))}
                    />
                  </label>
                  <label>
                    HAProxy stats portu
                    <input
                      type="number"
                      value={haproxyStatsPort}
                      onChange={(e) => setHaproxyStatsPort(e.target.value === "" ? "" : Number(e.target.value))}
                    />
                  </label>
                  <label>
                    keepalived VIP
                    <input value={keepalivedVip} onChange={(e) => setKeepalivedVip(e.target.value)} placeholder="10.0.0.50" />
                  </label>
                </>
              )}
              <label>
                Notlar
                <input value={notes} onChange={(e) => setNotes(e.target.value)} />
              </label>
            </div>
            <div className="form-actions">
              <button type="button" className="btn" onClick={goBack}>Geri</button>
              <button type="button" className="btn btn-primary" onClick={goNext}>İleri</button>
            </div>
          </>
        )}

        {currentKey === "nodes" && (
          <>
            <div className="activity-toolbar">
              <h3 className="chart-title" style={{ margin: 0 }}>
                {mode === "add-node" ? "Yeni düğümler" : isCluster ? "Düğümler" : "Sunucu ve instance"}
              </h3>
              <div style={{ display: "flex", gap: "0.5rem" }}>
                <button type="button" className="btn" onClick={testAllNodes}>Tümünü test et</button>
                {canAddNode && nodes.length < maxNewNodes && (
                  <button type="button" className="btn btn-primary" onClick={addNode}>+ Düğüm ekle</button>
                )}
              </div>
            </div>

            {mode === "create-group" && !isCluster && (
              <div className="form-grid" style={{ maxWidth: 480, marginBottom: "1rem" }}>
                <label>
                  Grup adı {REQUIRED}
                  <input
                    value={groupName}
                    onChange={(e) => setGroupName(e.target.value)}
                    className={fieldErrors.group_name ? "field-invalid" : ""}
                    placeholder="aapara-postgres-test"
                  />
                  {fieldErrors.group_name && <span className="field-error">{fieldErrors.group_name}</span>}
                </label>
                <label>
                  Ortam
                  <select value={environment} onChange={(e) => setEnvironment(e.target.value as GroupEnvironment)}>
                    <option value="prod">Prod</option>
                    <option value="preprod">Preprod</option>
                    <option value="test">Test</option>
                    <option value="dev">Dev</option>
                  </select>
                </label>
              </div>
            )}

            {nodes.map((node, idx) => (
              <div className="wizard-node-card" key={node.key}>
                <div className="wizard-node-card-head">
                  <strong>{mode === "add-node" || isCluster ? `Düğüm ${idx + 1}` : "Sunucu"}</strong>
                  {canAddNode && nodes.length > minNodesInStep && (
                    <button type="button" className="btn btn-danger btn-xs" onClick={() => removeNode(node.key)}>
                      Sil
                    </button>
                  )}
                </div>
                <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
                  <label>
                    Sunucu adı {REQUIRED}
                    <input
                      value={node.server_name}
                      onChange={(e) => updateNode(node.key, "server_name", e.target.value)}
                      className={fieldErrors[`node-${idx}-server_name`] ? "field-invalid" : ""}
                      placeholder={`${engine === "sqlserver" ? "winsvr" : "pgsvr"}-0${idx + 1}`}
                    />
                    {fieldErrors[`node-${idx}-server_name`] && (
                      <span className="field-error">{fieldErrors[`node-${idx}-server_name`]}</span>
                    )}
                  </label>
                  <label>
                    Hostname {REQUIRED}
                    <input
                      value={node.host}
                      onChange={(e) => updateNode(node.key, "host", e.target.value)}
                      className={fieldErrors[`node-${idx}-host`] ? "field-invalid" : ""}
                      placeholder="node.internal"
                    />
                    {fieldErrors[`node-${idx}-host`] && (
                      <span className="field-error">{fieldErrors[`node-${idx}-host`]}</span>
                    )}
                  </label>
                  <label>
                    IP adresi
                    <input
                      value={node.ip_address}
                      onChange={(e) => updateNode(node.key, "ip_address", e.target.value)}
                      placeholder="10.0.0.10"
                    />
                  </label>
                  <label>
                    İşletim sistemi
                    <select value={node.os} onChange={(e) => updateNode(node.key, "os", e.target.value as ServerOS)}>
                      <option value="linux">Linux</option>
                      <option value="windows">Windows</option>
                    </select>
                  </label>
                  <label>
                    Site
                    <select value={node.site} onChange={(e) => updateNode(node.key, "site", e.target.value as NodeSite)}>
                      <option value="primary">Ana DC</option>
                      <option value="disaster">Disaster (DR)</option>
                    </select>
                  </label>
                  {(mode === "add-node" || isCluster) && (
                    <label>
                      Rol
                      <select
                        value={node.role_hint}
                        onChange={(e) => updateNode(node.key, "role_hint", e.target.value as NodeRoleHint)}
                      >
                        <option value="unknown">Bilinmiyor</option>
                        <option value="primary">Primary</option>
                        <option value="replica">Replica</option>
                      </select>
                    </label>
                  )}
                  {engine === "sqlserver" && (
                    <label>
                      SQL Server instance adı
                      <input
                        value={node.instance_name}
                        onChange={(e) => updateNode(node.key, "instance_name", e.target.value)}
                        placeholder="MSSQLSERVER (varsayılan)"
                      />
                    </label>
                  )}
                  <label>
                    {engine === "sqlserver" ? "Instance portu" : "Port"} {REQUIRED}
                    <input
                      type="number"
                      value={node.port}
                      onChange={(e) => updateNode(node.key, "port", Number(e.target.value))}
                      className={fieldErrors[`node-${idx}-port`] ? "field-invalid" : ""}
                    />
                    {fieldErrors[`node-${idx}-port`] && (
                      <span className="field-error">{fieldErrors[`node-${idx}-port`]}</span>
                    )}
                  </label>
                  {engine === "sqlserver" && (
                    <label>
                      Kimlik doğrulama tipi
                      <select
                        value={node.authType}
                        onChange={(e) => updateNode(node.key, "authType", e.target.value as SqlServerAuthType)}
                      >
                        <option value="sql">SQL Server kimlik doğrulama</option>
                        <option value="windows">Windows (Integrated)</option>
                      </select>
                    </label>
                  )}
                  {(engine === "postgresql" || engine === "sqlserver") && (
                    <label>
                      Veritabanı adı
                      <input
                        value={node.database}
                        onChange={(e) => updateNode(node.key, "database", e.target.value)}
                        placeholder={defaultDatabase()}
                      />
                    </label>
                  )}
                  {engine === "postgresql" && (
                    <label>
                      SSL modu
                      <select value={node.sslMode} onChange={(e) => updateNode(node.key, "sslMode", e.target.value as PostgresSslMode)}>
                        <option value="disable">Devre dışı</option>
                        <option value="require">Gerekli (require)</option>
                      </select>
                    </label>
                  )}
                  {engine === "mongodb" && (
                    <>
                      <label>
                        Replica set adı
                        <input
                          value={node.replicaSet}
                          onChange={(e) => updateNode(node.key, "replicaSet", e.target.value)}
                          placeholder="rs0"
                        />
                      </label>
                      <label>
                        authSource
                        <input
                          value={node.authSource}
                          onChange={(e) => updateNode(node.key, "authSource", e.target.value)}
                          placeholder="admin"
                        />
                      </label>
                    </>
                  )}
                  {!(engine === "sqlserver" && node.authType === "windows") && (
                    <>
                      <label>
                        Kullanıcı adı {REQUIRED}
                        <input
                          value={node.db_username}
                          onChange={(e) => updateNode(node.key, "db_username", e.target.value)}
                          className={fieldErrors[`node-${idx}-db_username`] ? "field-invalid" : ""}
                        />
                        {fieldErrors[`node-${idx}-db_username`] && (
                          <span className="field-error">{fieldErrors[`node-${idx}-db_username`]}</span>
                        )}
                      </label>
                      <label>
                        Şifre
                        <input
                          type="password"
                          value={node.db_password}
                          onChange={(e) => updateNode(node.key, "db_password", e.target.value)}
                        />
                      </label>
                    </>
                  )}
                  <label>
                    Host agent URL (opsiyonel)
                    <input
                      value={node.agent_url}
                      onChange={(e) => updateNode(node.key, "agent_url", e.target.value)}
                      placeholder="http://node.internal:9105"
                    />
                  </label>
                  <label>
                    Host agent token (opsiyonel)
                    <input
                      type="password"
                      value={node.agent_token}
                      onChange={(e) => updateNode(node.key, "agent_token", e.target.value)}
                    />
                  </label>
                </div>
                <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.6rem", flexWrap: "wrap" }}>
                  <button type="button" className="btn btn-xs" disabled={node.testing} onClick={() => testNodeConnection(node.key)}>
                    {node.testing ? "Test ediliyor…" : "Bağlantıyı test et"}
                  </button>
                  {node.agent_url && (
                    <button type="button" className="btn btn-xs" disabled={node.agentTesting} onClick={() => testNodeAgent(node.key)}>
                      {node.agentTesting ? "Test ediliyor…" : "Agent'ı test et"}
                    </button>
                  )}
                  {node.testResult && (
                    <span className={node.testResult.ok ? "ok-text" : "warn-text"}>{node.testResult.message}</span>
                  )}
                  {node.agentTestResult && (
                    <span className={node.agentTestResult.ok ? "ok-text" : "warn-text"}>
                      Agent: {node.agentTestResult.message}
                    </span>
                  )}
                </div>
              </div>
            ))}

            <div className="form-actions">
              {mode === "create-group" && <button type="button" className="btn" onClick={goBack}>Geri</button>}
              <button type="button" className="btn btn-primary" onClick={goNext}>İleri</button>
            </div>
          </>
        )}

        {currentKey === "summary" && (
          <>
            <h3 className="chart-title">Özet</h3>
            {mode === "add-node" ? (
              <div className="wizard-summary-block">
                <h4>Var olan gruba ekleniyor</h4>
                <div className="wizard-summary-row"><span>Grup</span><strong>{existingGroup?.name}</strong></div>
                <div className="wizard-summary-row"><span>Motor</span><strong>{engine}</strong></div>
                <div className="wizard-summary-row"><span>Topoloji</span><strong>{topology}</strong></div>
                <div className="wizard-summary-row"><span>Mevcut düğüm sayısı</span><strong>{existingNodeCount}</strong></div>
              </div>
            ) : (
              <div className="wizard-summary-block">
                <h4>Grup</h4>
                <div className="wizard-summary-row"><span>Ad</span><strong>{groupName || "—"}</strong></div>
                <div className="wizard-summary-row"><span>Motor</span><strong>{engine}</strong></div>
                <div className="wizard-summary-row"><span>Topoloji</span><strong>{topology}</strong></div>
                <div className="wizard-summary-row"><span>Ortam</span><strong>{environment}</strong></div>
                {isCluster && (
                  <>
                    <div className="wizard-summary-row"><span>Cluster adı</span><strong>{clusterName || "—"}</strong></div>
                    <div className="wizard-summary-row"><span>Erişim adı</span><strong>{accessName || "—"}</strong></div>
                    <div className="wizard-summary-row"><span>VIP</span><strong>{vipAddress || "—"}</strong></div>
                    <div className="wizard-summary-row"><span>Listener port</span><strong>{listenerPort || "—"}</strong></div>
                  </>
                )}
              </div>
            )}
            {nodes.map((node, idx) => (
              <div className="wizard-summary-block" key={node.key}>
                <h4>{mode === "add-node" || isCluster ? `Düğüm ${idx + 1}` : "Sunucu"}</h4>
                <div className="wizard-summary-row"><span>Sunucu adı</span><strong>{node.server_name || "—"}</strong></div>
                <div className="wizard-summary-row"><span>Host</span><strong>{node.host || "—"}{node.ip_address ? ` (${node.ip_address})` : ""}</strong></div>
                <div className="wizard-summary-row"><span>Site / Rol</span><strong>{node.site} / {node.role_hint}</strong></div>
                <div className="wizard-summary-row"><span>Port / DB</span><strong>{node.port} / {node.database || defaultDatabase()}</strong></div>
                <div className="wizard-summary-row"><span>Kullanıcı</span><strong>{node.db_username || "—"}</strong></div>
                <div className="wizard-summary-row">
                  <span>Son bağlantı testi</span>
                  <strong className={node.testResult ? (node.testResult.ok ? "ok-text" : "warn-text") : ""}>
                    {node.testResult ? node.testResult.message : "test edilmedi"}
                  </strong>
                </div>
              </div>
            ))}
            <div className="form-actions">
              <button type="button" className="btn" onClick={goBack} disabled={saving}>Geri</button>
              <button type="button" className="btn btn-primary" onClick={onSave} disabled={saving}>
                {saving ? "Kaydediliyor…" : mode === "add-node" ? "Düğümleri ekle" : "Kaydet"}
              </button>
            </div>
          </>
        )}
      </div>
    </>
  );
}
