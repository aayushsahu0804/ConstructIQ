import { useEffect, useRef, useState } from 'react'
import axios from 'axios'
import './App.css'

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000'
const readThemePreference = () => {
  try { return localStorage.getItem('constructiq-theme') === 'dark' ? 'dark' : 'light' } catch { return 'light' }
}
// Edit these local defaults to customize the profile shown in the dashboard.
const projectProfile = {
  displayName: 'Project Administrator',
  role: 'Administrator',
  projectName: 'Construction Project',
}
const money = (value) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(value || 0)
const navItems = [
  { id: 'overview', label: 'Overview', icon: 'O' },
  { id: 'safety', label: 'Site Safety Audit', icon: 'S' },
  { id: 'crack', label: 'Crack Analysis', icon: 'C' },
  { id: 'planning', label: 'Planning & Valuation', icon: 'P' },
  { id: 'assistant', label: 'Construction Assistant', icon: 'A' },
]
const capabilities = [
  { id: 'safety', number: '01', title: 'Site Safety Audit', text: 'Review visible PPE and potential hazards from a site image.', action: 'Review site safety' },
  { id: 'crack', number: '02', title: 'Crack Analysis', text: 'Describe visible crack patterns and outline cautious next steps.', action: 'Review a crack' },
  { id: 'planning', number: '03', title: 'Planning & Valuation', text: 'Build an indicative estimate with editable materials and a basic plan.', action: 'Open the estimator' },
  { id: 'assistant', number: '04', title: 'Construction Assistant', text: 'Get construction guidance or draft a site report from your notes.', action: 'Ask the assistant' },
]

function useImageAnalysisState() {
  const requestVersion = useRef(0)
  const [file, setFile] = useState(null)
  const [previewUrl, setPreviewUrl] = useState('')
  const [analysis, setAnalysis] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!previewUrl) return undefined
    return () => URL.revokeObjectURL(previewUrl)
  }, [previewUrl])

  const selectImage = (selectedFile) => {
    if (!selectedFile) return
    requestVersion.current += 1
    setFile(selectedFile)
    setPreviewUrl(URL.createObjectURL(selectedFile))
    setAnalysis(null)
    setBusy(false)
    setError('')
  }

  const beginAnalysis = () => {
    requestVersion.current += 1
    setBusy(true)
    setError('')
    setAnalysis(null)
    return requestVersion.current
  }
  const isCurrentRequest = (version) => requestVersion.current === version
  const completeAnalysis = (version, result) => {
    if (isCurrentRequest(version)) setAnalysis(result)
  }
  const failAnalysis = (version, message) => {
    if (isCurrentRequest(version)) setError(message)
  }
  const finishAnalysis = (version) => {
    if (isCurrentRequest(version)) setBusy(false)
  }

  return { file, previewUrl, analysis, busy, error, selectImage, beginAnalysis, completeAnalysis, failAnalysis, finishAnalysis }
}

function App() {
  const [page, setPage] = useState('overview')
  const [health, setHealth] = useState(null)
  const [form, setForm] = useState({ area_sqft: '', location: 'Tier 1', building_type: 'Residential house', property_tier: 'Standard', completion: '' })
  const [materials, setMaterials] = useState([])
  const [plan, setPlan] = useState(null)
  const safetyAnalysis = useImageAnalysisState()
  const crackAnalysis = useImageAnalysisState()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [question, setQuestion] = useState('')
  const [reply, setReply] = useState(null)
  const [weatherLocation, setWeatherLocation] = useState('')
  const [weather, setWeather] = useState(null)
  const [weatherBusy, setWeatherBusy] = useState(false)
  const [weatherError, setWeatherError] = useState('')
  const [theme, setTheme] = useState(readThemePreference)

  useEffect(() => {
    axios.get(`${API}/api/health`)
      .then((response) => setHealth(response.data))
      .catch(() => setHealth({ status: 'offline', ai_configured: false }))
  }, [])

  useEffect(() => { try { localStorage.setItem('constructiq-theme', theme) } catch { /* Theme remains usable for this session. */ } }, [theme])

  const navigate = (destination) => {
    setPage(destination)
    setError('')
  }

  const loadPlan = async (event) => {
    event?.preventDefault()
    if (!Number(form.area_sqft) || Number(form.area_sqft) <= 0) {
      setError('Enter a built-up area greater than zero to estimate the project.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const response = await axios.post(`${API}/api/plan`, {
        area_sqft: Number(form.area_sqft),
        location: form.location,
        building_type: form.building_type,
        property_tier: form.property_tier,
        completion: form.completion === '' ? 0 : Number(form.completion),
        materials,
      })
      setPlan(response.data)
      setMaterials(response.data.materials)
    } catch (requestError) {
      setError(requestError.response?.data?.detail || 'Could not calculate the estimate. Check the inputs and API connection.')
    } finally {
      setBusy(false)
    }
  }

  const analyze = async (mode) => {
    const moduleState = mode === 'safety' ? safetyAnalysis : crackAnalysis
    if (!moduleState.file) return
    const requestVersion = moduleState.beginAnalysis()
    const data = new FormData()
    data.append('image', moduleState.file)
    try {
      const response = await axios.post(`${API}/api/analyze?mode=${mode}`, data)
      if (response.data.mode !== mode) throw new Error('Analysis mode mismatch')
      moduleState.completeAnalysis(requestVersion, response.data)
    } catch (requestError) {
      moduleState.failAnalysis(requestVersion, requestError.response?.data?.detail || 'Image analysis failed.')
    } finally {
      moduleState.finishAnalysis(requestVersion)
    }
  }

  const ask = async (kind = 'chat') => {
    if (!question.trim()) return
    setBusy(true)
    setError('')
    try {
      const response = await axios.post(`${API}/api/assistant`, { message: question, kind })
      setReply(response.data)
    } catch {
      setError('Assistant is unavailable. Check the backend connection.')
    } finally {
      setBusy(false)
    }
  }

  const loadWeather = async (event) => {
    event.preventDefault()
    const location = weatherLocation.trim()
    if (!location) {
      setWeatherError('Enter a city or location to look up current weather.')
      return
    }
    setWeatherBusy(true)
    setWeatherError('')
    setWeather(null)
    try {
      const response = await axios.get(`${API}/api/weather`, { params: { location } })
      if (!response.data.available) {
        setWeatherError(response.data.summary)
      } else {
        setWeather(response.data)
      }
    } catch (requestError) {
      setWeatherError(requestError.response?.data?.detail || 'Live weather is unavailable. Try again later.')
    } finally {
      setWeatherBusy(false)
    }
  }

  const exportAssessment = (report) => {
    if (!report) return
    const blob = new Blob([`${JSON.stringify(report, null, 2)}\n`], { type: 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `constructiq-${report.mode}-report.json`
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  }

  const pageTitles = {
    overview: 'Construction intelligence for clearer decisions.',
    safety: 'Site Safety Audit',
    crack: 'Crack Analysis',
    planning: 'Planning & Valuation',
    assistant: 'Construction Assistant',
  }
  const activeImageAnalysis = page === 'safety' ? safetyAnalysis : crackAnalysis

  return <div className={`app-shell theme-${theme}`}>
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">C<span>+</span></div>
    <div><b>CONSTRUCT<span>IQ</span></b><small className="brand-tagline">Built Different</small></div>
      </div>
      <div className="workspace-label">WORKSPACE</div>
      <nav aria-label="Main navigation">
        {navItems.map((item) => <button key={item.id} className={`nav-item ${page === item.id ? 'active' : ''}`} onClick={() => navigate(item.id)}>
          <i aria-hidden="true">{item.icon}</i><span>{item.label}</span>{item.id === 'safety' && <em>AI</em>}
        </button>)}
      </nav>
      <div className="sidebar-bottom">
        <div className="site-card"><span className="live-dot"/><div><b>{projectProfile.projectName}</b><small>PROJECT PROFILE</small></div><span className="dots">...</span></div>
        <div className="profile"><div className="avatar">PA</div><div><b>{projectProfile.displayName}</b><small>{projectProfile.role}</small></div></div>
      </div>
    </aside>

    <main className="main">
      <header className="topbar">
        <div className="crumb">CONSTRUCTIQ <span>/</span> {navItems.find((item) => item.id === page)?.label.toUpperCase()}</div>
        <div className="top-actions">
          <div className={`api-status ${health?.status === 'healthy' ? 'up' : ''}`}><i/>{health?.status === 'healthy' ? 'API connected' : 'API unavailable'}</div>
          <button className="utility-button" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} aria-pressed={theme === 'dark'} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}>{theme === 'dark' ? 'Light theme' : 'Dark theme'}</button>
          <button className="utility-button" onClick={() => window.print()}>Print / Save PDF</button>
        </div>
      </header>

      <section className="content">
        <div className="page-title"><div><div className="eyebrow">CONSTRUCTION FIELD INTELLIGENCE</div><h1>{pageTitles[page]}</h1><p>{page === 'overview' ? 'Practical tools for site observations, early estimates, and construction guidance.' : 'Use site information and review outputs with the responsible project professional.'}</p></div></div>
        {error && <div className="error-banner" role="alert">{error}<button onClick={() => setError('')} aria-label="Dismiss error">x</button></div>}

        {page === 'overview' && <>
          <section className="overview-hero panel">
            <span className="section-kicker">CONSTRUCTIQ</span>
            <h2>See the site clearly. Build with confidence.</h2>
            <p>ConstructIQ brings visual review, preliminary planning, and field assistance together in one workspace. Use each tool to organize observations and inform next steps; verify decisions against site records and qualified professional advice.</p>
          </section>
          <div className="capability-grid">
            {capabilities.map((item) => <article className="panel capability-card" key={item.id}>
              <span className="capability-number">{item.number}</span><h2>{item.title}</h2><p>{item.text}</p>
              <button className="text-button" onClick={() => navigate(item.id)}>{item.action} <span>-&gt;</span></button>
            </article>)}
          </div>
          <section className="panel weather-panel live-weather-panel">
            <div className="panel-head"><div><span className="section-kicker">LIVE WEATHER</span><h2>Check conditions for a location</h2></div><span className="weather-icon">Weather</span></div>
            <p className="panel-intro">Enter a city or location to retrieve current conditions and a three-day forecast. Data comes from Open-Meteo when its services are available.</p>
            <form className="weather-search no-print" onSubmit={loadWeather}>
              <label htmlFor="weather-location">City or location</label>
              <input id="weather-location" value={weatherLocation} onChange={(event) => setWeatherLocation(event.target.value)} placeholder="For example, Pune" maxLength={120}/>
              <button className="primary-button" disabled={weatherBusy}>{weatherBusy ? 'Checking...' : 'Get live weather'}</button>
            </form>
            {weatherBusy && <p className="weather-state" role="status">Looking up location and current conditions...</p>}
            {weatherError && <p className="weather-error" role="alert">{weatherError}</p>}
            {!weather && !weatherBusy && !weatherError && <p className="weather-state">No location selected. Weather is not guessed or filled with sample conditions.</p>}
            {weather && <div className="weather-result">
              <div className="weather-current"><span className="section-kicker">{weather.location} · observed {weather.current.observed_at}</span><strong>{weather.current.condition}, {weather.current.temperature_c} °C</strong><span>Precipitation {weather.current.precipitation_mm} mm · Wind {weather.current.wind_speed_kmh} km/h</span></div>
              <h3>Three-day forecast</h3>
              <div className="forecast-grid">{weather.forecast.map((day) => <article className="forecast-day" key={day.date}><b>{day.date}</b><span>{day.condition}</span><span>{day.minimum_c}–{day.maximum_c} °C</span><small>Precipitation chance: {day.precipitation_probability_percent}%</small></article>)}</div>
              <small className="weather-guidance">{weather.guidance.join(' ')}</small>
            </div>}
          </section>
        </>}

        {(page === 'safety' || page === 'crack') && <section className="tool-layout">
          <div className="panel upload-panel">
            <div className="panel-head"><div><span className="section-kicker">IMAGE REVIEW</span><h2>{page === 'safety' ? 'Site safety scan' : 'Crack image review'}</h2></div><span className="ai-tag">MULTIMODAL AI</span></div>
            <p className="panel-intro">{page === 'safety' ? 'Upload a clear site photo to review visible PPE and potential hazards.' : 'Upload a close, well-lit photo. Include a scale reference if dimensions matter.'}</p>
            <label className="dropzone" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); activeImageAnalysis.selectImage(event.dataTransfer.files[0]) }}>
              {activeImageAnalysis.previewUrl ? <img src={activeImageAnalysis.previewUrl} alt={page === 'safety' ? 'Selected safety audit image preview' : 'Selected crack analysis image preview'}/> : <><span className="upload-icon">+</span><b>Drop image here or <u>browse files</u></b><small>JPG, PNG or WEBP · up to 12 MB</small></>}
              <input type="file" accept="image/*" onChange={(event) => activeImageAnalysis.selectImage(event.target.files[0])}/>
            </label>
            <div className="notice">{page === 'safety' ? 'Visual observations do not verify regulatory or site compliance.' : 'A regular photo cannot provide precise crack dimensions without a scale.'}</div>
            <button className="primary-button" disabled={!activeImageAnalysis.file || activeImageAnalysis.busy} onClick={() => analyze(page)}>{activeImageAnalysis.busy ? 'Analyzing image...' : 'Analyze image'}</button>
            {activeImageAnalysis.error && <div className="error-banner module-error" role="alert">{activeImageAnalysis.error}</div>}
            {!health?.ai_configured && <small className="not-configured">AI provider not configured. Add GEMINI_API_KEY to backend/.env to enable analysis.</small>}
          </div>
          <div className="panel result-panel">
            <div className="report-heading"><div><span className="section-kicker">{page === 'safety' ? 'SITE SAFETY AUDIT' : 'CRACK ANALYSIS'}</span><h2>Findings</h2></div>{activeImageAnalysis.analysis && <button className="secondary-button no-print" onClick={() => exportAssessment(activeImageAnalysis.analysis)}>Export JSON</button>}</div>
            {activeImageAnalysis.analysis ? <>
              <div className="result-summary">{activeImageAnalysis.analysis.summary}<small>Source: {activeImageAnalysis.analysis.provider}</small></div>
              {activeImageAnalysis.analysis.findings.map((finding, index) => <div className="finding" key={index}><div><span className={`severity ${finding.severity}`}>{finding.severity.toUpperCase()}</span><b>{finding.title}</b></div><p>{finding.detail}</p><small>Confidence {Math.round(finding.confidence * 100)}% · Action: {finding.action}</small></div>)}
              {activeImageAnalysis.analysis.limitations.map((limitation, index) => <small className="limitation" key={index}>{limitation}</small>)}
            </> : <div className="empty-result"><div>...</div><b>Your assessment will appear here</b><p>Upload an image and start analysis. Findings include confidence and next actions.</p></div>}
          </div>
        </section>}

        {page === 'planning' && <section className="planning-layout">
          <form className="panel form-panel" onSubmit={loadPlan}>
            <div className="panel-head"><div><span className="section-kicker">PROJECT INPUTS</span><h2>Estimate your project</h2></div><span className="tiny-pill">EDITABLE</span></div>
            <div className="form-grid">
              <label className="wide">Built-up area <span>sq ft</span><input type="number" min="1" max="10000000" step="any" value={form.area_sqft} onChange={(event) => setForm({ ...form, area_sqft: event.target.value })} required/></label>
              <label>City tier<select value={form.location} onChange={(event) => setForm({ ...form, location: event.target.value })}><option>Tier 1</option><option>Tier 2</option><option>Tier 3</option></select></label>
              <label>Property tier<select value={form.property_tier} onChange={(event) => setForm({ ...form, property_tier: event.target.value })}><option>Value</option><option>Standard</option><option>Premium</option><option>Luxury</option></select></label>
              <label className="wide">Structure type<select value={form.building_type} onChange={(event) => setForm({ ...form, building_type: event.target.value })}><option>Residential house</option><option>Apartment building</option><option>Commercial building</option><option>Warehouse / industrial</option><option>Renovation</option></select></label>
              <label className="wide">Project completion <span>optional %</span><input type="number" min="0" max="100" step="any" value={form.completion} onChange={(event) => setForm({ ...form, completion: event.target.value })} placeholder="Not supplied"/></label>
            </div>
            <div className="material-input-heading"><h3 className="editable-heading">Material inputs · quantity and unit rate</h3>{materials.length > 0 && <button type="button" className="text-button no-print" onClick={() => { setMaterials([]); setPlan(null) }}>Use calculated quantities</button>}</div>
            {materials.length ? materials.map((material, index) => <div className="material-input" key={material.name}>
              <b>{material.name}</b><input aria-label={`${material.name} quantity`} type="number" min="0" step="any" value={material.quantity} onChange={(event) => setMaterials(materials.map((item, itemIndex) => itemIndex === index ? { ...item, quantity: Number(event.target.value) } : item))}/><small>{material.unit}</small><span>₹</span><input aria-label={`${material.name} unit rate`} type="number" min="0" step="any" value={material.rate} onChange={(event) => setMaterials(materials.map((item, itemIndex) => itemIndex === index ? { ...item, rate: Number(event.target.value) } : item))}/>
            </div>) : <p className="muted materials-note">Calculated benchmark quantities appear here after the first estimate; edit quantities and rates before recalculating.</p>}
            <button className="primary-button" disabled={busy || !Number(form.area_sqft)}>{busy ? 'Calculating...' : 'Calculate indicative estimate'}</button>
          </form>
          <div className={`panel plan-results ${plan ? 'has-plan' : ''}`}>
            <span className="section-kicker">PLANNING OUTPUT</span><h2>{plan ? money(plan.estimated_total_inr) : 'Ready to estimate'}</h2><p className="muted">Indicative construction cost estimate · INR</p>
            {plan ? <>
              <p className="estimate-disclaimer">Planning benchmark only. This is not a professional valuation, tender, or quotation.</p>
              <div className="cost-list">{Object.entries(plan.cost_breakdown).map(([name, value]) => <div key={name}><span>{name}</span><b>{money(value)}</b><i><em style={{ width: `${plan.estimated_total_inr ? value / plan.estimated_total_inr * 100 : 0}%` }}/></i></div>)}</div>
              <h3>Material quantities</h3>{plan.materials.map((material) => <div className="material-row" key={material.name}><span>{material.name}</span><b>{material.quantity.toLocaleString()} {material.unit}</b><span>{money(material.rate)} / {material.unit}</span></div>)}
              <div className="assumptions">{plan.assumptions.map((assumption, index) => <p key={index}>{assumption}</p>)}</div>
              <h3>Basic schedule & resources</h3>{plan.tasks.map((task) => <div className="task-row" key={task.task}><b>{task.task}</b><span>{task.duration}</span><small>{task.resource}</small></div>)}
            </> : <div className="empty-result"><p>Enter an area to calculate an indicative cost breakdown, editable material quantities, and a basic schedule.</p></div>}
          </div>
        </section>}

        {page === 'assistant' && <section className="assistant-layout">
          <div className="panel assistant-panel"><span className="section-kicker">FIELD ASSISTANT</span><h2>Ask a construction question</h2><p className="panel-intro">Offline guidance is clearly labeled. Configure Gemini for model-generated help.</p>
            <div className="prompt-chips"><button onClick={() => setQuestion('What should I include in a daily site report?')}>Daily report checklist</button><button onClick={() => setQuestion('How should a newly observed structural crack be handled?')}>Crack response steps</button><button onClick={() => setQuestion('What weather precautions should a site team review today?')}>Weather safety</button></div>
            <textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Describe your construction question or paste site notes..."/>
            <div className="assistant-actions"><button className="secondary-button" disabled={busy || !question.trim()} onClick={() => ask('daily_report')}>Draft daily report</button><button className="primary-button" disabled={busy || !question.trim()} onClick={() => ask('chat')}>{busy ? 'Thinking...' : 'Ask assistant'}</button></div>
            {reply && <div className="assistant-reply"><span className="ai-tag">{reply.provider}</span><p>{reply.answer}</p>{reply.guidance.map((guidance, index) => <p key={index}>• {guidance}</p>)}</div>}
          </div>
          <div className="panel safety-note"><div className="note-icon">i</div><span className="section-kicker">RELIABLE FIELD USE</span><h2>Keep decisions grounded.</h2><p>AI suggestions support planning and observation. Confirm engineering decisions, compliance, and local requirements with qualified professionals and site records.</p><div className="note-rule"/><b>Live weather</b><p>Use the location lookup on Overview for current provider data, or check a trusted local forecast before weather-sensitive work.</p></div>
        </section>}

        <footer className="app-footer"><span>CONSTRUCTIQ · FIELD INTELLIGENCE</span><span className="footer-links"><a href="https://github.com/aayushsahu0804" target="_blank" rel="noopener noreferrer">GitHub profile</a><a href="https://github.com/aayushsahu0804/ConstructIQ" target="_blank" rel="noopener noreferrer">ConstructIQ repository</a></span></footer>
      </section>
    </main>
  </div>
}

export default App
