// ╔══════════════════════════════════════════════════════════════╗
// ║  Requesty Analytics Dashboard                               ║
// ║  ── 2 lignes à modifier avant déploiement ──                ║
// ╚══════════════════════════════════════════════════════════════╝
import { useState, useEffect, useCallback } from "react";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  LineChart, Line, PieChart, Pie, Cell
} from "recharts";
import {
  AlertTriangle, TrendingDown, RefreshCw, Download,
  Database, Zap, DollarSign, Users, Activity, Search,
  ChevronUp, ChevronDown, X, Clock
} from "lucide-react";

// ┌─────────────────────────────────────────────────────────────┐
// │  ✏️  CONFIGURATION — modifier ces 2 lignes uniquement       │
// └─────────────────────────────────────────────────────────────┘
const API_BASE = "https://analytics.srv1474234.hstgr.cloud";  // URL du VPS
const API_TOKEN  = "REMPLACE_PAR_TON_TOKEN";           // Token choisi au déploiement
// ─────────────────────────────────────────────────────────────

const COLORS = ["#6EE7B7","#34D399","#10B981","#059669","#F59E0B","#EF4444","#8B5CF6","#3B82F6","#EC4899","#14B8A6"];

const fmt     = n => { if(n==null||isNaN(n))return"–"; if(n>=1e6)return(n/1e6).toFixed(1)+"M"; if(n>=1e3)return(n/1e3).toFixed(1)+"K"; return Number(n).toFixed(0); };
const fmtCost = n => !n?"$0.00":n>=1?`$${Number(n).toFixed(2)}`:`$${Number(n).toFixed(4)}`;
const fmtDate = s => { try{return new Date(s).toLocaleDateString("fr-FR",{day:"2-digit",month:"short"});}catch{return s?.slice(0,10)||"–";} };

// ── API helper — injecte le token automatiquement ─────────────
async function api(path) {
  const sep = path.includes("?") ? "&" : "?";
  const url = `${API_BASE}${path}${API_TOKEN ? `${sep}token=${API_TOKEN}` : ""}`;
  const r = await fetch(url, {
    headers: API_TOKEN ? { "Authorization": `Bearer ${API_TOKEN}` } : {}
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

// ── Metric card ───────────────────────────────────────────────
function Card({ icon:Icon, label, value, sub, color="#10B981" }) {
  return (
    <div style={{ background:"rgba(255,255,255,0.03)", border:`1px solid ${color}22`, borderRadius:12, padding:"16px 20px", position:"relative", overflow:"hidden" }}>
      <div style={{ position:"absolute", top:-18, right:-18, width:64, height:64, borderRadius:"50%", background:color+"14" }}/>
      <div style={{ display:"flex", alignItems:"center", gap:7, marginBottom:7 }}>
        <Icon size={13} color={color}/>
        <span style={{ fontSize:10, color:"#6b7280", fontFamily:"monospace", textTransform:"uppercase", letterSpacing:1 }}>{label}</span>
      </div>
      <div style={{ fontSize:24, fontWeight:700, color:"#f0fdf4", fontFamily:"'Space Grotesk',sans-serif" }}>{value}</div>
      {sub && <div style={{ fontSize:11, color:"#6b7280", marginTop:3 }}>{sub}</div>}
    </div>
  );
}

// ── Churn badge ───────────────────────────────────────────────
function Badge({ risk }) {
  const c = { over:{label:"⚡ Sur-conso",bg:"#7f1d1d",color:"#fca5a5",border:"#ef4444"}, under:{label:"❄️ Sous-conso",bg:"#1e3a5f",color:"#93c5fd",border:"#3b82f6"}, normal:{label:"✓ Normal",bg:"#064e3b",color:"#6ee7b7",border:"#10b981"} }[risk]||{label:"–",bg:"#111",color:"#666",border:"#333"};
  return <span style={{ padding:"2px 8px", borderRadius:6, fontSize:11, fontWeight:600, background:c.bg, color:c.color, border:`1px solid ${c.border}` }}>{c.label}</span>;
}

// ── Org table ─────────────────────────────────────────────────
function OrgTable({ data, median, search, sortCol, sortDir, onSort }) {
  const cols = [
    {key:"org_name",label:"Organisation"},
    {key:"total_tokens",label:"Tokens"},
    {key:"total_cost",label:"Coût"},
    {key:"models",label:"Modèles"},
    {key:"request_count",label:"Requêtes"},
    {key:"last_seen",label:"Dernière activité"},
    {key:"risk",label:"Risque churn"},
  ];
  let rows = data.map(r => ({...r, risk:!median?"normal":r.total_tokens>median*2?"over":r.total_tokens<median*.3?"under":"normal"}));
  if (search) { const s=search.toLowerCase(); rows=rows.filter(r=>r.org_name?.toLowerCase().includes(s)||r.models?.toLowerCase().includes(s)); }
  rows.sort((a,b) => {
    const va=a[sortCol],vb=b[sortCol];
    if(typeof va==="number") return sortDir==="asc"?va-vb:vb-va;
    return sortDir==="asc"?String(va||"").localeCompare(String(vb||"")):String(vb||"").localeCompare(String(va||""));
  });
  return (
    <div style={{ overflowX:"auto" }}>
      <table style={{ width:"100%", borderCollapse:"collapse", fontSize:13 }}>
        <thead><tr>{cols.map(c=>(
          <th key={c.key} onClick={()=>onSort(c.key)} style={{ padding:"10px 14px", textAlign:"left", cursor:"pointer", color:sortCol===c.key?"#10B981":"#6b7280", fontFamily:"monospace", fontSize:10, fontWeight:500, textTransform:"uppercase", letterSpacing:.5, borderBottom:"1px solid rgba(110,231,183,0.1)", userSelect:"none", whiteSpace:"nowrap" }}>
            <span style={{ display:"inline-flex", alignItems:"center", gap:3 }}>{c.label}{sortCol===c.key&&(sortDir==="asc"?<ChevronUp size={10}/>:<ChevronDown size={10}/>)}</span>
          </th>
        ))}</tr></thead>
        <tbody>
          {!rows.length&&<tr><td colSpan={7} style={{ padding:40,textAlign:"center",color:"#6b7280" }}>Aucune donnée</td></tr>}
          {rows.map((r,i)=>(
            <tr key={r.org_id||i} style={{ borderBottom:"1px solid rgba(110,231,183,0.05)" }}
              onMouseEnter={e=>e.currentTarget.style.background="rgba(110,231,183,0.04)"}
              onMouseLeave={e=>e.currentTarget.style.background="transparent"}>
              <td style={{ padding:"10px 14px",color:"#f0fdf4",fontWeight:500,maxWidth:180,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap" }}>{r.org_name}</td>
              <td style={{ padding:"10px 14px",color:"#6EE7B7",fontFamily:"monospace" }}>{fmt(r.total_tokens)}</td>
              <td style={{ padding:"10px 14px",color:"#F59E0B",fontFamily:"monospace" }}>{fmtCost(r.total_cost)}</td>
              <td style={{ padding:"10px 14px",color:"#9ca3af",fontSize:11,maxWidth:140,overflow:"hidden",textOverflow:"ellipsis" }}>{r.models?.split(",").slice(0,2).join(", ")}</td>
              <td style={{ padding:"10px 14px",color:"#9ca3af" }}>{r.request_count?.toLocaleString()}</td>
              <td style={{ padding:"10px 14px",color:"#6b7280",fontSize:12 }}>{fmtDate(r.last_seen)}</td>
              <td style={{ padding:"10px 14px" }}><Badge risk={r.risk}/></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Dashboard ─────────────────────────────────────────────────
export default function App() {
  const [loading, setLoading]   = useState(true);
  const [syncing, setSyncing]   = useState(false);
  const [connected, setConnected] = useState(false);
  const [tab, setTab]           = useState("overview");
  const [period, setPeriod]     = useState("30d");
  const [search, setSearch]     = useState("");
  const [sortCol, setSortCol]   = useState("total_tokens");
  const [sortDir, setSortDir]   = useState("desc");
  const [error, setError]       = useState("");

  const [health, setHealth]       = useState(null);
  const [overview, setOverview]   = useState(null);
  const [orgData, setOrgData]     = useState([]);
  const [modelData, setModelData] = useState([]);
  const [timeseries, setTS]       = useState([]);
  const [churn, setChurn]         = useState(null);

  const since = () => {
    const d = new Date();
    d.setDate(d.getDate() - ({1:1,7:7,30:30,90:90}[period]||30));
    return d.toISOString();
  };

  const loadAll = useCallback(async () => {
    setLoading(true); setError("");
    const s = since();
    try {
      const [h, ov, orgs, models, ts, ch] = await Promise.all([
        api("/health"),
        api(`/analytics/overview?since=${s}`),
        api(`/analytics/by-org?since=${s}`),
        api(`/analytics/by-model?since=${s}`),
        api(`/analytics/timeseries?since=${s}`),
        api(`/analytics/churn-risk?since=${s}`),
      ]);
      setHealth(h); setOverview(ov); setOrgData(orgs);
      setModelData(models); setTS(ts); setChurn(ch);
      setConnected(true);
    } catch(e) {
      setConnected(false);
      setError(e.message.includes("401") ? "Token invalide — vérifie API_TOKEN dans le code." : e.message);
    }
    setLoading(false);
  }, [period]);

  useEffect(() => { loadAll(); }, [period]);

  const doSync = async () => {
    setSyncing(true); setError("");
    try {
      await fetch(`${API_BASE}/sync?token=${API_TOKEN}`, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ period }),
      });
      await loadAll();
    } catch(e) { setError("Sync échouée: "+e.message); }
    setSyncing(false);
  };

  const exportCSV = () => window.open(`${API_BASE}/export/csv?token=${API_TOKEN}`);
  const handleSort = col => { if(sortCol===col) setSortDir(d=>d==="asc"?"desc":"asc"); else { setSortCol(col); setSortDir("desc"); }};

  return (
    <div style={{ minHeight:"100vh", background:"#030712", color:"#f0fdf4", fontFamily:"'Inter',sans-serif" }}>
      <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet"/>

      {/* ── Header ── */}
      <div style={{ borderBottom:"1px solid rgba(110,231,183,0.1)", padding:"12px 26px", display:"flex", alignItems:"center", justifyContent:"space-between", background:"rgba(255,255,255,0.02)", position:"sticky", top:0, zIndex:100, backdropFilter:"blur(10px)" }}>
        <div style={{ display:"flex", alignItems:"center", gap:14 }}>
          <span style={{ fontSize:18 }}>⚡</span>
          <span style={{ fontFamily:"'Space Grotesk',sans-serif", fontWeight:700, fontSize:16 }}>Requesty Analytics</span>
          <span style={{ display:"flex", alignItems:"center", gap:5, fontSize:10 }}>
            <span style={{ width:7, height:7, borderRadius:"50%", background: connected?"#10B981":"#EF4444", display:"inline-block", boxShadow: connected?"0 0 6px #10B981":"none" }}/>
            <span style={{ color: connected?"#10B981":"#EF4444" }}>{connected?"Connecté":"Hors ligne"}</span>
          </span>
          {health && (
            <span style={{ fontSize:11, color:"#6b7280", fontFamily:"monospace" }}>
              <Database size={10} style={{ verticalAlign:"middle" }}/> {health.records?.toLocaleString()} enr.
            </span>
          )}
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:9 }}>
          {health?.last_sync && (
            <span style={{ fontSize:11, color:"#6b7280", display:"flex", alignItems:"center", gap:4 }}>
              <Clock size={10}/> {new Date(health.last_sync).toLocaleTimeString("fr-FR")}
            </span>
          )}
          <select value={period} onChange={e=>setPeriod(e.target.value)} style={{ background:"rgba(255,255,255,0.05)", border:"1px solid rgba(110,231,183,0.2)", borderRadius:6, padding:"4px 8px", color:"#9ca3af", fontSize:12 }}>
            <option value="1d">24h</option>
            <option value="7d">7j</option>
            <option value="30d">30j</option>
            <option value="90d">90j</option>
          </select>
          <button onClick={doSync} disabled={syncing} style={{ display:"flex", alignItems:"center", gap:6, padding:"6px 13px", borderRadius:8, background:"rgba(16,185,129,0.15)", border:"1px solid rgba(16,185,129,0.4)", color:"#10B981", fontSize:12, cursor:syncing?"not-allowed":"pointer", fontWeight:600 }}>
            <RefreshCw size={12} style={{ animation:syncing?"spin 1s linear infinite":"none" }}/>{syncing?"Sync...":"Synchroniser"}
          </button>
          <button onClick={exportCSV} style={{ display:"flex", alignItems:"center", gap:5, padding:"6px 13px", borderRadius:8, background:"rgba(245,158,11,0.1)", border:"1px solid rgba(245,158,11,0.3)", color:"#F59E0B", fontSize:12, cursor:"pointer" }}>
            <Download size={12}/> CSV
          </button>
        </div>
      </div>

      {/* ── Error bar ── */}
      {error && (
        <div style={{ margin:"12px 26px 0", padding:"10px 14px", background:"rgba(239,68,68,0.1)", border:"1px solid rgba(239,68,68,0.3)", borderRadius:10, color:"#fca5a5", fontSize:12, display:"flex", justifyContent:"space-between" }}>
          ⚠️ {error}
          <button onClick={()=>setError("")} style={{ background:"none", border:"none", color:"#fca5a5", cursor:"pointer" }}><X size={13}/></button>
        </div>
      )}

      {/* ── Not connected ── */}
      {!loading && !connected && (
        <div style={{ margin:"40px auto", maxWidth:520, padding:"28px 32px", background:"rgba(239,68,68,0.06)", border:"1px solid rgba(239,68,68,0.25)", borderRadius:14 }}>
          <div style={{ color:"#fca5a5", fontWeight:600, fontSize:15, marginBottom:12 }}>Impossible de joindre le serveur</div>
          <div style={{ color:"#6b7280", fontSize:13, lineHeight:1.7 }}>
            Vérifie que le service tourne sur le VPS :
          </div>
          <div style={{ marginTop:12, padding:"12px 14px", background:"rgba(0,0,0,0.4)", borderRadius:8, fontFamily:"monospace", fontSize:12, color:"#e2e8f0", lineHeight:2 }}>
            <div style={{ color:"#6b7280" }}># Sur ton VPS :</div>
            <div>systemctl status requesty-analytics</div>
            <div>journalctl -u requesty-analytics -f</div>
          </div>
        </div>
      )}

      {/* ── Loading ── */}
      {loading && (
        <div style={{ textAlign:"center", padding:80, color:"#6b7280" }}>
          <span style={{ fontSize:28, animation:"spin 1s linear infinite", display:"inline-block" }}>⚡</span>
          <div style={{ marginTop:14 }}>Chargement...</div>
        </div>
      )}

      {/* ── Dashboard ── */}
      {!loading && connected && overview && (
        <div style={{ padding:"22px 26px" }}>

          {/* KPIs */}
          <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(165px,1fr))", gap:13, marginBottom:26 }}>
            <Card icon={Zap}           label="Total Tokens"       value={fmt(overview.total_tokens)}   sub={`${Number(overview.total_requests||0).toLocaleString()} requêtes`} color="#10B981"/>
            <Card icon={DollarSign}    label="Coût total"         value={fmtCost(overview.total_cost)} sub="période sélectionnée" color="#F59E0B"/>
            <Card icon={Users}         label="Orgs / Users"       value={overview.unique_orgs||0}      sub={`${overview.unique_models||0} modèles`} color="#8B5CF6"/>
            <Card icon={AlertTriangle} label="Sur-consommateurs"  value={churn?.over?.length||0}       sub="risque churn ↑ coût" color="#EF4444"/>
            <Card icon={TrendingDown}  label="Sous-consommateurs" value={churn?.under?.length||0}      sub="risque churn ↓ usage" color="#3B82F6"/>
            <Card icon={Activity}      label="Latence moy."       value={overview.avg_latency?`${Math.round(overview.avg_latency)}ms`:"–"} sub={overview.cache_hits?`${overview.cache_hits} cache hits`:""} color="#34D399"/>
          </div>

          {/* Tabs */}
          <div style={{ display:"flex", gap:3, marginBottom:18, borderBottom:"1px solid rgba(110,231,183,0.1)" }}>
            {["overview","organisations","modèles","churn"].map(t=>(
              <button key={t} onClick={()=>setTab(t)} style={{ padding:"7px 16px", background:tab===t?"rgba(16,185,129,0.15)":"transparent", border:"none", borderBottom:tab===t?"2px solid #10B981":"2px solid transparent", color:tab===t?"#10B981":"#6b7280", fontSize:13, fontWeight:500, cursor:"pointer", textTransform:"capitalize", transition:"all .15s" }}>{t}</button>
            ))}
          </div>

          {/* ── Overview ── */}
          {tab==="overview" && (
            <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:18 }}>
              {[{title:"Tokens / jour",key:"tokens",stroke:"#10B981",type:"bar"},{title:"Coût / jour ($)",key:"cost",stroke:"#F59E0B",type:"line"}].map(({title,key,stroke,type})=>(
                <div key={key} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(110,231,183,0.1)", borderRadius:12, padding:18 }}>
                  <h3 style={{ margin:"0 0 14px", fontSize:11, color:"#9ca3af", fontFamily:"monospace", textTransform:"uppercase", letterSpacing:1 }}>{title}</h3>
                  <ResponsiveContainer width="100%" height={190}>
                    {type==="bar"
                      ? <BarChart data={timeseries}><XAxis dataKey="period" tick={{fill:"#6b7280",fontSize:9}}/><YAxis tick={{fill:"#6b7280",fontSize:9}} tickFormatter={v=>fmt(v)}/><Tooltip formatter={v=>[fmt(v)+" tokens","Tokens"]} contentStyle={{background:"#111827",border:`1px solid ${stroke}33`}}/><Bar dataKey={key} fill={stroke} radius={[3,3,0,0]}/></BarChart>
                      : <LineChart data={timeseries}><XAxis dataKey="period" tick={{fill:"#6b7280",fontSize:9}}/><YAxis tick={{fill:"#6b7280",fontSize:9}} tickFormatter={v=>"$"+v.toFixed(2)}/><Tooltip formatter={v=>[fmtCost(v),"Coût"]} contentStyle={{background:"#111827",border:`1px solid ${stroke}33`}}/><Line type="monotone" dataKey={key} stroke={stroke} strokeWidth={2} dot={false}/></LineChart>
                    }
                  </ResponsiveContainer>
                </div>
              ))}
              <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(110,231,183,0.1)", borderRadius:12, padding:18 }}>
                <h3 style={{ margin:"0 0 14px", fontSize:11, color:"#9ca3af", fontFamily:"monospace", textTransform:"uppercase", letterSpacing:1 }}>Top consommateurs</h3>
                <ResponsiveContainer width="100%" height={190}>
                  <BarChart data={orgData.slice(0,10).map(o=>({name:o.org_name?.slice(0,14)||o.org_id,tokens:o.total_tokens}))} layout="vertical">
                    <XAxis type="number" tick={{fill:"#6b7280",fontSize:9}} tickFormatter={v=>fmt(v)}/>
                    <YAxis type="category" dataKey="name" tick={{fill:"#9ca3af",fontSize:10}} width={100}/>
                    <Tooltip formatter={v=>[fmt(v)+" tokens"]} contentStyle={{background:"#111827",border:"1px solid rgba(110,231,183,0.2)"}}/>
                    <Bar dataKey="tokens" fill="#6EE7B7" radius={[0,3,3,0]}/>
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(110,231,183,0.1)", borderRadius:12, padding:18 }}>
                <h3 style={{ margin:"0 0 14px", fontSize:11, color:"#9ca3af", fontFamily:"monospace", textTransform:"uppercase", letterSpacing:1 }}>Par modèle</h3>
                <ResponsiveContainer width="100%" height={190}>
                  <PieChart>
                    <Pie data={modelData.slice(0,8).map(m=>({name:m.model?.split("/").pop(),value:m.total_tokens}))} dataKey="value" cx="50%" cy="50%" outerRadius={78} label={({name,percent})=>`${name} ${(percent*100).toFixed(0)}%`} labelLine={false}>
                      {modelData.slice(0,8).map((_,i)=><Cell key={i} fill={COLORS[i%COLORS.length]}/>)}
                    </Pie>
                    <Tooltip formatter={v=>[fmt(v)+" tokens"]} contentStyle={{background:"#111827",border:"1px solid rgba(110,231,183,0.2)"}}/>
                  </PieChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          {/* ── Organisations ── */}
          {tab==="organisations" && (
            <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(110,231,183,0.1)", borderRadius:12, overflow:"hidden" }}>
              <div style={{ padding:"13px 18px", borderBottom:"1px solid rgba(110,231,183,0.08)", display:"flex", gap:10, alignItems:"center" }}>
                <Search size={13} color="#6b7280"/>
                <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Rechercher org, modèle..." style={{ background:"none", border:"none", outline:"none", color:"#f0fdf4", fontSize:13, flex:1 }}/>
                {search&&<button onClick={()=>setSearch("")} style={{background:"none",border:"none",cursor:"pointer",color:"#6b7280"}}><X size={12}/></button>}
              </div>
              <OrgTable data={orgData} median={churn?.median} search={search} sortCol={sortCol} sortDir={sortDir} onSort={handleSort}/>
            </div>
          )}

          {/* ── Modèles ── */}
          {tab==="modèles" && (
            <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:18 }}>
              <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(110,231,183,0.1)", borderRadius:12, padding:18 }}>
                <h3 style={{ margin:"0 0 14px", fontSize:11, color:"#9ca3af", fontFamily:"monospace", textTransform:"uppercase", letterSpacing:1 }}>Tokens par modèle</h3>
                <ResponsiveContainer width="100%" height={300}>
                  <BarChart data={modelData.slice(0,10).map(m=>({name:m.model?.split("/").pop(),tokens:m.total_tokens}))}>
                    <XAxis dataKey="name" tick={{fill:"#6b7280",fontSize:9}} angle={-20} textAnchor="end" height={48}/>
                    <YAxis tick={{fill:"#6b7280",fontSize:9}} tickFormatter={v=>fmt(v)}/>
                    <Tooltip formatter={v=>[fmt(v)+" tokens"]} contentStyle={{background:"#111827",border:"1px solid rgba(110,231,183,0.2)"}}/>
                    <Bar dataKey="tokens" radius={[4,4,0,0]}>{modelData.slice(0,10).map((_,i)=><Cell key={i} fill={COLORS[i%COLORS.length]}/>)}</Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(110,231,183,0.1)", borderRadius:12, padding:18 }}>
                <h3 style={{ margin:"0 0 14px", fontSize:11, color:"#9ca3af", fontFamily:"monospace", textTransform:"uppercase", letterSpacing:1 }}>Détail</h3>
                <div style={{ maxHeight:300, overflowY:"auto" }}>
                  {modelData.map(m=>(
                    <div key={m.model} style={{ display:"flex", justifyContent:"space-between", padding:"9px 0", borderBottom:"1px solid rgba(110,231,183,0.05)" }}>
                      <div><div style={{ fontSize:12, color:"#f0fdf4", fontFamily:"monospace" }}>{m.model}</div><div style={{ fontSize:11, color:"#6b7280" }}>{m.request_count} req · {m.unique_orgs} org</div></div>
                      <div style={{ textAlign:"right" }}><div style={{ fontSize:12, color:"#6EE7B7" }}>{fmt(m.total_tokens)} tok</div><div style={{ fontSize:11, color:"#F59E0B" }}>{fmtCost(m.total_cost)}</div></div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* ── Churn ── */}
          {tab==="churn" && churn && (
            <div style={{ display:"flex", flexDirection:"column", gap:18 }}>
              <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:16 }}>
                {[
                  {list:churn.over,  bg:"rgba(239,68,68,0.06)",  border:"rgba(239,68,68,0.3)",  title:`⚡ Sur-consommateurs (${churn.over.length})`,  tc:"#fca5a5", action:"📚 Optimiser prompts, caching, modèles moins chers", desc:`>2× médiane (${fmt(churn.median)} tok)`},
                  {list:churn.under, bg:"rgba(59,130,246,0.06)", border:"rgba(59,130,246,0.3)", title:`❄️ Sous-consommateurs (${churn.under.length})`, tc:"#93c5fd", action:"🚀 Onboarding, cas d'usage, support proactif",         desc:"<0.3× médiane — faible adoption"},
                ].map(({list,bg,border,title,tc,action,desc})=>(
                  <div key={title} style={{ background:bg, border:`1px solid ${border}`, borderRadius:12, padding:18 }}>
                    <div style={{ color:tc, fontSize:14, fontWeight:600, marginBottom:8 }}>{title}</div>
                    <p style={{ color:"#6b7280", fontSize:12, margin:"0 0 12px", lineHeight:1.65 }}>{desc}<br/><em>{action}</em></p>
                    {list.slice(0,8).map(o=>(
                      <div key={o.org_id} style={{ display:"flex", justifyContent:"space-between", padding:"7px 0", borderBottom:`1px solid ${border}` }}>
                        <div><div style={{ fontSize:13, color:"#f0fdf4" }}>{o.org_name}</div><div style={{ fontSize:11, color:"#6b7280" }}>{o.requests} req · ×{o.median_ratio}</div></div>
                        <div style={{ textAlign:"right", fontSize:12, color:tc, fontFamily:"monospace" }}>{fmt(o.tokens)} tok<br/>{fmtCost(o.cost)}</div>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
              <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(110,231,183,0.1)", borderRadius:12, padding:18 }}>
                <h3 style={{ margin:"0 0 6px", fontSize:11, color:"#9ca3af", fontFamily:"monospace", textTransform:"uppercase", letterSpacing:1 }}>
                  Distribution — Médiane: {fmt(churn.median)} · Seuil haut: {fmt(churn.thresholds?.high)} · Seuil bas: {fmt(churn.thresholds?.low)}
                </h3>
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={[...churn.over,...churn.normal,...churn.under].sort((a,b)=>b.tokens-a.tokens).slice(0,25).map(o=>({name:o.org_name?.slice(0,11),tokens:o.tokens}))}>
                    <XAxis dataKey="name" tick={{fill:"#6b7280",fontSize:9}}/>
                    <YAxis tick={{fill:"#6b7280",fontSize:9}} tickFormatter={v=>fmt(v)}/>
                    <Tooltip formatter={v=>[fmt(v)+" tokens"]} contentStyle={{background:"#111827",border:"1px solid rgba(110,231,183,0.2)"}}/>
                    <Bar dataKey="tokens" radius={[3,3,0,0]}>
                      {[...churn.over,...churn.normal,...churn.under].sort((a,b)=>b.tokens-a.tokens).slice(0,25).map((o,i)=>(
                        <Cell key={i} fill={o.tokens>churn.thresholds?.high?"#EF4444":o.tokens<churn.thresholds?.low?"#3B82F6":"#10B981"}/>
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
                <div style={{ display:"flex", gap:18, marginTop:8, justifyContent:"center" }}>
                  {[{c:"#EF4444",l:"Sur-conso"},{c:"#10B981",l:"Normal"},{c:"#3B82F6",l:"Sous-conso"}].map(({c,l})=>(
                    <span key={l} style={{ fontSize:11, color:"#6b7280", display:"flex", alignItems:"center", gap:4 }}><span style={{ width:9,height:9,borderRadius:2,background:c,display:"inline-block" }}/>{l}</span>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
      <style>{`@keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}*{scrollbar-width:thin;scrollbar-color:#10B981 #030712}select option{background:#1f2937}`}</style>
    </div>
  );
}
