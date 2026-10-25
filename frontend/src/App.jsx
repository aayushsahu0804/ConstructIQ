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

const getScoreTier = (score) => {
  if (score === null || score === undefined) {
    return { status: 'unavailable', label: 'Score Unavailable', barClass: 'unavailable' }
  }
  if (score < 50) {
    return { status: 'critical', label: 'Critical Hazard Level — Urgent Action Required', barClass: 'critical' }
  }
  if (score < 75) {
    return { status: 'warning', label: 'Needs Attention — Moderate Hazards Observed', barClass: 'warning' }
  }
  if (score < 90) {
    return { status: 'moderate', label: 'Fair Practices — Minor Improvements Advised', barClass: 'moderate' }
  }
  return { status: 'good', label: 'High Compliance — Continued Monitoring Recommended', barClass: 'good' }
}

const getPPEStatusDisplay = (status) => {
  switch (status) {
    case 'observed':
      return { label: 'Observed', className: 'observed' }
    case 'not_observed':
      return { label: 'Not observed', className: 'not_observed' }
    default:
      return { label: 'Not assessable', className: 'not_assessable' }
  }
}

function FormattedAssistantAnswer({ text }) {
  if (!text) return null
  const paragraphs = text.split(/\n\s*\n/)
  return (
    <div className="formatted-answer">
      {paragraphs.map((p, idx) => {
        const trimmed = p.trim()
        if (trimmed.startsWith('### ') || trimmed.startsWith('## ') || trimmed.startsWith('# ')) {
          const headingText = trimmed.replace(/^#+\s*/, '')
          return <h4 key={idx} className="answer-heading">{headingText}</h4>
        }
        if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
          const items = trimmed.split('\n').filter(Boolean)
          return (
            <ul key={idx} className="answer-list">
              {items.map((item, itemIdx) => (
                <li key={itemIdx}>{item.replace(/^[-*]\s*/, '').replace(/\*\*(.*?)\*\*/g, '$1')}</li>
              ))}
            </ul>
          )
        }
        return <p key={idx}>{trimmed}</p>
      })}
    </div>
  )
}

function tryParseAssistantJSON(text) {
  if (!text || typeof text !== 'string') return null
  const trimmed = text.trim()

  // 1. Markdown code block ```json ... ```
  const codeBlockMatch = trimmed.match(/```(?:json)?\s*([\s\S]*?)\s*```/i)
  if (codeBlockMatch) {
    try {
      const parsed = JSON.parse(codeBlockMatch[1].trim())
      if (parsed && typeof parsed === 'object') return parsed
    } catch {
      // ignore
    }
  }

  // 2. Direct JSON parse
  if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
    try {
      const parsed = JSON.parse(trimmed)
      if (parsed && typeof parsed === 'object') return parsed
    } catch {
      // Attempt python dict conversion (single quotes)
      try {
        const converted = trimmed
          .replace(/'/g, '"')
          .replace(/None/g, 'null')
          .replace(/True/g, 'true')
          .replace(/False/g, 'false')
        const parsed = JSON.parse(converted)
        if (parsed && typeof parsed === 'object') return parsed
      } catch {
        // ignore
      }
    }
  }

  // 3. Embedded JSON object inside text
  const firstBrace = trimmed.indexOf('{')
  const lastBrace = trimmed.lastIndexOf('}')
  if (firstBrace !== -1 && lastBrace > firstBrace) {
    try {
      const snippet = trimmed.slice(firstBrace, lastBrace + 1)
      const parsed = JSON.parse(snippet)
      if (parsed && typeof parsed === 'object') return parsed
    } catch {
      // ignore
    }
  }

  return null
}

function parseStructuredReport(rawAnswer) {
  const parsed = tryParseAssistantJSON(rawAnswer)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return null
  }

  // If parsed object has an inner "answer" that is itself a JSON string
  if (typeof parsed.answer === 'string' && (parsed.answer.trim().startsWith('{') || parsed.answer.trim().startsWith('```'))) {
    const inner = tryParseAssistantJSON(parsed.answer)
    if (inner && typeof inner === 'object' && !Array.isArray(inner)) {
      return parseStructuredReport(inner)
    }
  }

  const summary = parsed.summary || parsed.executive_summary || parsed.situation_summary || parsed.overview || (typeof parsed.answer === 'string' && !parsed.answer.trim().startsWith('{') ? parsed.answer : null)

  const rawFindings = parsed.findings || parsed.priority_findings || parsed.hazards || parsed.risks || parsed.observations
  const findings = Array.isArray(rawFindings) ? rawFindings.map((f) => {
    if (typeof f === 'string') return { title: f, severity: 'medium', detail: f }
    return {
      title: f.title || f.hazard || f.name || 'Site Finding',
      severity: (f.severity || 'medium').toLowerCase(),
      confidence: typeof f.confidence === 'number' ? f.confidence : null,
      location: f.location || f.area || null,
      detail: f.detail || f.explanation || f.description || null,
      risk_explanation: f.risk_explanation || f.risk || f.potential_risk || null,
      action: f.action || f.recommended_action || f.corrective_action || f.mitigation || null,
    }
  }) : []

  const rawActions = parsed.recommended_actions || parsed.actions || parsed.task_sequence || parsed.action_items || parsed.recommendations || parsed.steps || parsed.tasks
  const actions = Array.isArray(rawActions) ? rawActions.map((a) => {
    if (typeof a === 'string') return { text: a, urgent: a.toLowerCase().includes('urgent') || a.toLowerCase().includes('immediate') || a.toLowerCase().includes('stop') }
    return {
      text: a.action || a.task || a.text || a.description || JSON.stringify(a),
      urgent: Boolean(a.urgent || a.priority === 'urgent' || a.priority === 'immediate' || a.priority === 'high'),
    }
  }) : []

  const rawStopWork = parsed.stop_work_conditions || parsed.stop_work || parsed.critical_alerts || parsed.red_flags
  const stopWork = Array.isArray(rawStopWork) ? rawStopWork.map((s) => typeof s === 'string' ? s : (s.condition || s.text || JSON.stringify(s))) : (typeof rawStopWork === 'string' ? [rawStopWork] : [])

  const rawPPE = parsed.ppe_checklist || parsed.ppe_evaluations || parsed.ppe
  const ppeChecklist = Array.isArray(rawPPE) ? rawPPE.map((p) => {
    if (typeof p === 'string') return { item: p, status: 'observed', explanation: '' }
    const st = (p.status || 'not_assessable').toLowerCase().replace(/\s+/g, '_')
    const normalizedStatus = (st === 'observed' || st === 'not_observed') ? st : 'not_assessable'
    return {
      item: p.item || p.gear || p.name || 'PPE Item',
      status: normalizedStatus,
      explanation: p.explanation || p.detail || p.notes || '',
    }
  }) : []

  const rawLimits = parsed.limitations || parsed.disclaimer || parsed.caveats
  const limitations = Array.isArray(rawLimits) ? rawLimits.map((l) => typeof l === 'string' ? l : (l.text || JSON.stringify(l))) : (typeof rawLimits === 'string' ? [rawLimits] : [])

  const score = typeof parsed.safety_score === 'number' ? parsed.safety_score : (typeof parsed.score === 'number' ? parsed.score : null)
  const siteContextData = (parsed.site_context && typeof parsed.site_context === 'object') ? parsed.site_context : null

  if (!summary && findings.length === 0 && actions.length === 0 && ppeChecklist.length === 0 && score === null && stopWork.length === 0) {
    return null
  }

  return {
    summary,
    findings,
    actions,
    stopWork,
    ppeChecklist,
    limitations,
    score,
    siteContextData,
  }
}

function AssistantReport({ reply, siteContext, assistantWeather }) {
  const [copied, setCopied] = useState(false)
  if (!reply) return null

  const structured = parseStructuredReport(reply.answer)

  // A. Build Site Context items (display only actually provided items)
  const contextItems = []
  const ctx = structured?.siteContextData || {}
  const activity = ctx.activity || siteContext?.activity?.trim()
  if (activity) contextItems.push({ label: 'Activity', value: activity })

  const labour = ctx.labour_count !== undefined && ctx.labour_count !== null ? ctx.labour_count : (siteContext?.labour_count ? `${siteContext.labour_count} workers` : null)
  if (labour) contextItems.push({ label: 'Workforce', value: typeof labour === 'number' ? `${labour} workers` : labour })

  const condition = ctx.site_condition || (siteContext?.site_condition && siteContext.site_condition !== 'Normal' ? siteContext.site_condition : (siteContext?.site_condition || null))
  if (condition) contextItems.push({ label: 'Conditions', value: condition })

  const location = ctx.location || siteContext?.location?.trim()
  if (location) contextItems.push({ label: 'Location', value: location })

  const weather = ctx.weather_summary || (assistantWeather ? `${assistantWeather.location}: ${assistantWeather.current?.condition}, ${assistantWeather.current?.temperature_c} °C` : (siteContext?.weather_input?.trim() || null))
  if (weather) contextItems.push({ label: 'Weather', value: weather })

  const timing = ctx.work_timing || siteContext?.work_timing
  if (timing) contextItems.push({ label: 'Shift Timing', value: timing })

  const notes = ctx.notes || siteContext?.notes?.trim()
  if (notes) contextItems.push({ label: 'Notes', value: notes })

  const handleCopy = () => {
    let text = 'CONSTRUCTIQ FIELD INTELLIGENCE REPORT\n'
    text += `Provider: ${reply.provider}\n`
    text += `Timestamp: ${new Date().toLocaleString()}\n\n`

    if (contextItems.length > 0) {
      text += '--- SITE CONTEXT ---\n'
      contextItems.forEach((c) => { text += `• ${c.label}: ${c.value}\n` })
      text += '\n'
    }

    if (structured?.summary) {
      text += `--- EXECUTIVE SUMMARY ---\n${structured.summary}\n\n`
    } else if (!structured && reply.answer) {
      text += `--- REPORT ---\n${reply.answer}\n\n`
    }

    if (structured?.findings?.length > 0) {
      text += '--- PRIORITY FINDINGS ---\n'
      structured.findings.forEach((f, i) => {
        text += `${i + 1}. [${(f.severity || 'low').toUpperCase()}] ${f.title}\n`
        if (f.location) text += `   Location: ${f.location}\n`
        if (f.detail) text += `   Detail: ${f.detail}\n`
        if (f.risk_explanation) text += `   Risk: ${f.risk_explanation}\n`
        if (f.action) text += `   Action: ${f.action}\n`
      })
      text += '\n'
    }

    if (structured?.stopWork?.length > 0) {
      text += '--- STOP-WORK CONDITIONS ---\n'
      structured.stopWork.forEach((s) => { text += `⚠️ STOP: ${s}\n` })
      text += '\n'
    }

    if (structured?.actions?.length > 0) {
      text += '--- RECOMMENDED ACTIONS ---\n'
      structured.actions.forEach((a, i) => {
        text += `${i + 1}. ${a.urgent ? '[URGENT] ' : ''}${a.text}\n`
      })
      text += '\n'
    }

    if (structured?.ppeChecklist?.length > 0) {
      text += '--- PPE CHECKLIST ---\n'
      structured.ppeChecklist.forEach((p) => {
        text += `• ${p.item}: ${p.status.toUpperCase()} - ${p.explanation || 'N/A'}\n`
      })
      text += '\n'
    }

    if (reply.guidance?.length > 0) {
      text += '--- GUIDANCE & NEXT STEPS ---\n'
      reply.guidance.forEach((g) => { text += `• ${g}\n` })
      text += '\n'
    }

    text += '--- DISCLAIMER ---\n'
    text += 'ConstructIQ decision support only. Confirm all operations with the licensed site engineer or safety officer.'

    if (navigator?.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(() => {
        setCopied(true)
        setTimeout(() => setCopied(false), 2000)
      }).catch(() => setCopied(false))
    }
  }

  const handleExport = () => {
    const exportData = {
      report_type: 'constructiq_assistant_report',
      exported_at: new Date().toISOString(),
      provider: reply.provider,
      site_context: Object.fromEntries(contextItems.map((c) => [c.label.toLowerCase().replace(/\s+/g, '_'), c.value])),
      structured_data: structured,
      raw_answer: reply.answer,
      guidance: reply.guidance || [],
    }
    const blob = new Blob([`${JSON.stringify(exportData, null, 2)}\n`], { type: 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `constructiq-assistant-report-${Date.now()}.json`
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  }

  return (
    <div className="assistant-report-shell">
      <div className="assistant-report-topbar">
        <div className="report-topbar-left">
          <span className="ai-tag">{reply.provider}</span>
          {structured?.score !== null && structured?.score !== undefined && (
            <span className="assistant-score-pill">
              Safety Rating: <b>{structured.score}/100</b>
            </span>
          )}
        </div>
        <div className="assistant-report-actions no-print">
          <button type="button" className="utility-button" onClick={handleCopy} title="Copy readable report to clipboard">
            {copied ? '✓ Copied' : 'Copy response'}
          </button>
          <button type="button" className="utility-button" onClick={handleExport} title="Export report to JSON">
            Export report
          </button>
        </div>
      </div>

      {contextItems.length > 0 && (
        <div className="assistant-context-panel">
          <div className="context-panel-title">
            <span className="section-kicker">SITE CONTEXT</span>
            <small>Active Parameters</small>
          </div>
          <div className="context-chips-row">
            {contextItems.map((c, i) => (
              <span className="context-chip" key={i}>
                <span className="chip-label">{c.label}:</span>
                <span className="chip-value">{c.value}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {structured ? (
        <div className="assistant-structured-body">
          {structured.summary && (
            <div className="assistant-section executive-summary-section">
              <span className="section-kicker">EXECUTIVE SUMMARY</span>
              <p className="executive-summary-text">{structured.summary}</p>
            </div>
          )}

          {structured.stopWork && structured.stopWork.length > 0 && (
            <div className="stop-work-card">
              <div className="stop-work-head">
                <span className="stop-icon">⚠️</span>
                <div>
                  <strong>STOP-WORK CONDITIONS & CRITICAL TRIGGERS</strong>
                  <small>Halt relevant operations immediately pending inspection</small>
                </div>
              </div>
              <ul className="stop-work-list">
                {structured.stopWork.map((item, idx) => (
                  <li key={idx}>{item}</li>
                ))}
              </ul>
            </div>
          )}

          {structured.findings && structured.findings.length > 0 && (
            <div className="assistant-section priority-findings-section">
              <div className="section-head">
                <div>
                  <span className="section-kicker">PRIORITY FINDINGS</span>
                  <h3>Identified Site Risks & Observations ({structured.findings.length})</h3>
                </div>
                <span className="tiny-pill">SEVERITY CODED</span>
              </div>
              <div className="assistant-findings-grid">
                {structured.findings.map((f, i) => {
                  const sev = f.severity || 'medium'
                  return (
                    <div className={`assistant-finding-card sev-${sev}`} key={i}>
                      <div className="finding-card-header">
                        <span className={`severity-tag ${sev}`}>{sev.toUpperCase()}</span>
                        <h4 className="finding-title">{f.title}</h4>
                        {f.confidence !== null && f.confidence !== undefined && (
                          <span className="finding-conf">
                            {Math.round(f.confidence <= 1 ? f.confidence * 100 : f.confidence)}% conf
                          </span>
                        )}
                      </div>

                      {f.location && (
                        <div className="finding-meta-row">
                          <span className="meta-label">Location / Area:</span>
                          <span className="meta-val">{f.location}</span>
                        </div>
                      )}

                      {f.detail && <p className="finding-detail-text">{f.detail}</p>}

                      {f.risk_explanation && (
                        <div className="finding-risk-callout">
                          <strong>Potential Risk:</strong> {f.risk_explanation}
                        </div>
                      )}

                      {f.action && (
                        <div className="finding-action-callout">
                          <strong>Recommended Action:</strong> {f.action}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {structured.actions && structured.actions.length > 0 && (
            <div className="assistant-section actions-section">
              <div className="section-head">
                <div>
                  <span className="section-kicker">RECOMMENDED ACTIONS</span>
                  <h3>Actionable Task Sequence & Precautions</h3>
                </div>
              </div>
              <div className="actions-checklist">
                {structured.actions.map((act, idx) => (
                  <div className={`action-check-item ${act.urgent ? 'urgent-item' : ''}`} key={idx}>
                    <span className="action-step-num">{idx + 1}</span>
                    <div className="action-content">
                      {act.urgent && <span className="urgent-badge">URGENT</span>}
                      <span>{act.text}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {structured.ppeChecklist && structured.ppeChecklist.length > 0 && (
            <div className="assistant-section ppe-checklist-section">
              <div className="section-head">
                <div>
                  <span className="section-kicker">PERSONAL PROTECTIVE EQUIPMENT</span>
                  <h3>Required Gear & Safety Protocol</h3>
                </div>
                <span className="tiny-pill">EVIDENCE-BASED</span>
              </div>
              <p className="section-subtitle">
                Evidence-based status. "Not assessable" indicates equipment cannot be verified from available information and must not be assumed compliant.
              </p>
              <div className="ppe-grid">
                {structured.ppeChecklist.map((item, idx) => {
                  const badge = getPPEStatusDisplay(item.status)
                  return (
                    <div className="ppe-item-card" key={idx}>
                      <div className="ppe-item-top">
                        <span className="ppe-name">{item.item}</span>
                        <span className={`ppe-status ${badge.className}`}>{badge.label}</span>
                      </div>
                      {item.explanation && <p className="ppe-explanation">{item.explanation}</p>}
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {reply.guidance && reply.guidance.length > 0 && (
            <div className="assistant-guidance-box">
              <h4>Key Guidance & Next Steps</h4>
              {reply.guidance.map((guidance, index) => <p key={index}>• {guidance}</p>)}
            </div>
          )}

          <div className="assistant-section limitations-section">
            <span className="section-kicker">LIMITATIONS & PROFESSIONAL REVIEW</span>
            {structured.limitations && structured.limitations.length > 0 && (
              <div className="limitations-list">
                {structured.limitations.map((lim, idx) => (
                  <small className="limitation" key={idx}>• {lim}</small>
                ))}
              </div>
            )}
            <div className="notice warning-notice">
              <strong>Decision Support Notice:</strong> Assistant recommendations support field observation and task planning. They do not replace local statutory building codes, structural engineering evaluations, or safety procedures. Always obtain formal clearance from the designated site engineer or safety officer.
            </div>
          </div>
        </div>
      ) : (
        <div className="assistant-text-body">
          <FormattedAssistantAnswer text={reply.answer} />

          {reply.guidance && reply.guidance.length > 0 && (
            <div className="assistant-guidance-box">
              <h4>Key Guidance & Next Steps</h4>
              {reply.guidance.map((guidance, index) => <p key={index}>• {guidance}</p>)}
            </div>
          )}

          <div className="notice warning-notice">
            <strong>Decision Support Notice:</strong> Assistant recommendations provide decision support based on reported inputs. Always confirm with the project engineer, designated safety officer, or local building authority before executing safety-critical operations.
          </div>
        </div>
      )}
    </div>
  )
}

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

  const [siteContext, setSiteContext] = useState({
    activity: '',
    labour_count: '',
    site_condition: 'Normal',
    location: '',
    weather_input: '',
    work_timing: 'Morning shift (08:00 - 16:00)',
    notes: '',
  })
  const [assistantWeather, setAssistantWeather] = useState(null)
  const [assistantWeatherBusy, setAssistantWeatherBusy] = useState(false)
  const [assistantWeatherError, setAssistantWeatherError] = useState('')

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

  const fetchAssistantWeather = async () => {
    const loc = (siteContext.location || weatherLocation || '').trim()
    if (!loc) {
      setAssistantWeatherError('Enter a site location to fetch live weather.')
      return
    }
    setAssistantWeatherBusy(true)
    setAssistantWeatherError('')
    setAssistantWeather(null)
    try {
      const response = await axios.get(`${API}/api/weather`, { params: { location: loc } })
      if (!response.data.available) {
        setAssistantWeatherError(response.data.summary || 'Live weather unavailable for this location.')
      } else {
        setAssistantWeather(response.data)
      }
    } catch (requestError) {
      setAssistantWeatherError(requestError.response?.data?.detail || 'Live weather retrieval failed. Continuing with entered inputs.')
    } finally {
      setAssistantWeatherBusy(false)
    }
  }

  const ask = async (kind = 'chat') => {
    if (!question.trim()) return
    setBusy(true)
    setError('')
    try {
      const weatherSummary = assistantWeather
        ? `${assistantWeather.location}: ${assistantWeather.current.condition}, ${assistantWeather.current.temperature_c} °C, precip ${assistantWeather.current.precipitation_mm} mm, wind ${assistantWeather.current.wind_speed_kmh} km/h (live weather)`
        : (siteContext.weather_input?.trim() || null)

      const hasContext = Boolean(
        siteContext.activity.trim() ||
        siteContext.labour_count ||
        siteContext.site_condition ||
        siteContext.location.trim() ||
        weatherSummary ||
        siteContext.work_timing ||
        siteContext.notes.trim()
      )

      const contextPayload = hasContext ? {
        activity: siteContext.activity.trim() || null,
        labour_count: siteContext.labour_count ? Number(siteContext.labour_count) : null,
        site_condition: siteContext.site_condition || null,
        location: siteContext.location.trim() || null,
        weather_summary: weatherSummary,
        work_timing: siteContext.work_timing || null,
        notes: siteContext.notes.trim() || null,
      } : null

      const response = await axios.post(`${API}/api/assistant`, {
        message: question,
        kind,
        site_context: contextPayload,
      })
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
    const exportData = {
      project: projectProfile.projectName,
      exported_at: new Date().toISOString(),
      constructiq_version: '2.0',
      ...report,
    }
    const blob = new Blob([`${JSON.stringify(exportData, null, 2)}\n`], { type: 'application/json;charset=utf-8' })
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
            {activeImageAnalysis.analysis ? (
              page === 'safety' ? (
                <div className="safety-results">
                  {/* 1. Safety Score out of 100 */}
                  <div className="safety-score-card">
                    <div className="safety-score-header">
                      <span className="section-kicker">SITE SAFETY AUDIT SCORE</span>
                      <span className={`score-badge ${getScoreTier(activeImageAnalysis.analysis.safety_score).status}`}>
                        {getScoreTier(activeImageAnalysis.analysis.safety_score).label}
                      </span>
                    </div>
                    <div className="score-display">
                      {activeImageAnalysis.analysis.safety_score !== null && activeImageAnalysis.analysis.safety_score !== undefined ? (
                        <>
                          <span className="score-number">{activeImageAnalysis.analysis.safety_score}</span>
                          <span className="score-total">/100</span>
                        </>
                      ) : (
                        <span className="score-unavailable-text">Score unavailable</span>
                      )}
                    </div>
                    {activeImageAnalysis.analysis.safety_score !== null && activeImageAnalysis.analysis.safety_score !== undefined && (
                      <div className="score-bar-track">
                        <div
                          className={`score-bar-fill ${getScoreTier(activeImageAnalysis.analysis.safety_score).barClass}`}
                          style={{ width: `${Math.max(5, Math.min(100, activeImageAnalysis.analysis.safety_score))}%` }}
                        />
                      </div>
                    )}
                    <p className="score-caption">
                      {activeImageAnalysis.analysis.safety_score !== null
                        ? 'Score calculated deterministically from identified site hazards, severity weighting, and observed PPE compliance.'
                        : 'Visual evidence in the image is insufficient to produce a defensible quantitative safety score.'}
                    </p>
                  </div>

                  {/* 2. Overall Safety Assessment */}
                  <div className="overall-assessment-card">
                    <div className="panel-head">
                      <span className="section-kicker">OVERALL SAFETY ASSESSMENT</span>
                      <small className="ai-tag">Source: {activeImageAnalysis.analysis.provider}</small>
                    </div>
                    <p className="assessment-summary">{activeImageAnalysis.analysis.summary}</p>
                  </div>

                  {/* 3. PPE Checklist */}
                  <div className="ppe-section">
                    <div className="panel-head">
                      <div>
                        <span className="section-kicker">MANDATORY PPE CHECKLIST</span>
                        <h3>Visible Gear & Equipment Assessment</h3>
                      </div>
                      <span className="tiny-pill">EVIDENCE-BASED</span>
                    </div>
                    <p className="section-subtitle">Items are only marked "Not observed" when workers or active work areas are visible without required gear. Where line-of-sight is obstructed or workers are absent, items are categorized as "Not assessable".</p>
                    <div className="ppe-grid">
                      {(activeImageAnalysis.analysis.ppe_checklist || []).map((ppe, index) => {
                        const badge = getPPEStatusDisplay(ppe.status)
                        return (
                          <div className="ppe-item-card" key={index}>
                            <div className="ppe-item-top">
                              <span className="ppe-name">{ppe.item}</span>
                              <span className={`ppe-status ${badge.className}`}>{badge.label}</span>
                            </div>
                            <p className="ppe-explanation">{ppe.explanation}</p>
                          </div>
                        )
                      })}
                    </div>
                  </div>

                  {/* 4. Hazards and Corrective Actions */}
                  <div className="hazards-section">
                    <div className="panel-head">
                      <div>
                        <span className="section-kicker">OBSERVED HAZARDS & CORRECTIVE ACTIONS</span>
                        <h3>Identified Site Risks ({activeImageAnalysis.analysis.findings?.length || 0})</h3>
                      </div>
                      <span className="tiny-pill">PRIORITY ORDERED</span>
                    </div>
                    <div className="findings-list">
                      {(activeImageAnalysis.analysis.findings || []).map((finding, index) => (
                        <div className="finding-card" key={index}>
                          <div className="finding-head">
                            <span className={`severity ${finding.severity}`}>{finding.severity.toUpperCase()}</span>
                            <b>{finding.title}</b>
                          </div>
                          {finding.location && (
                            <div className="finding-meta">
                              <strong>Visible evidence & location:</strong> {finding.location}
                            </div>
                          )}
                          <p className="finding-detail">{finding.detail}</p>
                          {finding.risk_explanation && (
                            <div className="finding-risk">
                              <strong>Potential risk:</strong> {finding.risk_explanation}
                            </div>
                          )}
                          <div className="finding-action">
                            <strong>Recommended corrective action:</strong> {finding.action}
                          </div>
                          <div className="finding-foot">
                            <span>Confidence: {Math.round(finding.confidence * 100)}%</span>
                            <span className="hazard-rank">Priority #{index + 1}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* 5. Prioritized Corrective Actions */}
                  {activeImageAnalysis.analysis.prioritized_actions && activeImageAnalysis.analysis.prioritized_actions.length > 0 && (
                    <div className="prioritized-actions-section">
                      <div className="panel-head">
                        <div>
                          <span className="section-kicker">ACTION PLAN</span>
                          <h3>Prioritized Corrective Actions</h3>
                        </div>
                      </div>
                      <p className="section-subtitle">Immediate precautions and routine safety improvements for observed conditions.</p>
                      <div className="prioritized-list">
                        {activeImageAnalysis.analysis.prioritized_actions.map((action, index) => (
                          <div className="prioritized-item" key={index}>
                            <span className="prioritized-item-num">{index + 1}</span>
                            <span>{action}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* 6. Limitations & Professional Review */}
                  <div className="limitations-section">
                    <div className="panel-head">
                      <span className="section-kicker">LIMITATIONS & PROFESSIONAL REVIEW</span>
                    </div>
                    <div className="limitations-list">
                      {(activeImageAnalysis.analysis.limitations || []).map((limitation, index) => (
                        <small className="limitation" key={index}>• {limitation}</small>
                      ))}
                    </div>
                    <div className="notice warning-notice">
                      <strong>Visual Observation Notice:</strong> Findings are based solely on two-dimensional visual evidence in the uploaded image. Observations do not verify statutory or regulatory compliance. Always obtain formal on-site inspection and clearance from a certified Safety Officer or Structural Engineer.
                    </div>
                  </div>
                </div>
              ) : (
                <div className="crack-results">
                  <div className="result-summary">{activeImageAnalysis.analysis.summary}<small>Source: {activeImageAnalysis.analysis.provider}</small></div>
                  <div className="findings-section">
                    <h3>Observed Crack Findings</h3>
                    {(activeImageAnalysis.analysis.findings || []).map((finding, index) => (
                      <div className="finding-card" key={index}>
                        <div className="finding-head"><span className={`severity ${finding.severity}`}>{finding.severity.toUpperCase()}</span><b>{finding.title}</b></div>
                        <p className="finding-detail">{finding.detail}</p>
                        <div className="finding-action"><strong>Recommended action:</strong> {finding.action}</div>
                        <small className="finding-foot">Confidence {Math.round(finding.confidence * 100)}%</small>
                      </div>
                    ))}
                  </div>
                  <div className="limitations-section">
                    <h3>Assessment Limitations</h3>
                    {(activeImageAnalysis.analysis.limitations || []).map((limitation, index) => <small className="limitation" key={index}>{limitation}</small>)}
                    <div className="notice">A photograph cannot provide precise crack dimensions without an in-frame scale reference. Consult a licensed structural engineer for active or load-bearing cracks.</div>
                  </div>
                </div>
              )
            ) : (
              <div className="empty-result"><div>...</div><b>Your assessment will appear here</b><p>Upload an image and start analysis. Findings include confidence and next actions.</p></div>
            )}
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
          <div className="panel assistant-panel">
            <div className="panel-head">
              <div>
                <span className="section-kicker">CONTEXT-AWARE SITE ASSISTANT</span>
                <h2>Ask a construction question</h2>
              </div>
              <span className="ai-tag">DECISION SUPPORT</span>
            </div>
            <p className="panel-intro">Supply site context below so AI guidance combines your planned activity, labour, ground conditions, and local weather.</p>

            <div className="context-form-card">
              <div className="context-form-header">
                <div>
                  <span className="section-kicker">SITE CONTEXT (OPTIONAL)</span>
                  <h3>Field Conditions & Activity Setup</h3>
                </div>
                {(siteContext.activity || siteContext.labour_count || siteContext.location || assistantWeather || siteContext.weather_input || siteContext.notes) && (
                  <button
                    type="button"
                    className="text-button no-print"
                    onClick={() => {
                      setSiteContext({ activity: '', labour_count: '', site_condition: 'Normal', location: '', weather_input: '', work_timing: 'Morning shift (08:00 - 16:00)', notes: '' })
                      setAssistantWeather(null)
                      setAssistantWeatherError('')
                    }}
                  >
                    Reset context
                  </button>
                )}
              </div>

              <div className="context-grid">
                <label>
                  Planned construction activity
                  <input
                    list="activity-suggestions"
                    value={siteContext.activity}
                    onChange={(e) => setSiteContext({ ...siteContext, activity: e.target.value })}
                    placeholder="e.g. Slab casting, excavation..."
                  />
                  <datalist id="activity-suggestions">
                    <option value="Slab concreting / casting" />
                    <option value="Deep excavation & trenching" />
                    <option value="Reinforcement & rebar fixing" />
                    <option value="Brick masonry & block work" />
                    <option value="External scaffolding & formwork" />
                    <option value="Roof slab waterproofing" />
                    <option value="Plastering & external finishing" />
                    <option value="Structural steel erection" />
                  </datalist>
                </label>

                <label>
                  Labour count
                  <input
                    type="number"
                    min="0"
                    max="1000"
                    value={siteContext.labour_count}
                    onChange={(e) => setSiteContext({ ...siteContext, labour_count: e.target.value })}
                    placeholder="e.g. 10 workers"
                  />
                </label>

                <label>
                  Site conditions
                  <select
                    value={siteContext.site_condition}
                    onChange={(e) => setSiteContext({ ...siteContext, site_condition: e.target.value })}
                  >
                    <option value="Normal">Normal site conditions</option>
                    <option value="Wet / slippery surfaces">Wet / slippery surfaces</option>
                    <option value="Congested / restricted space">Congested / restricted space</option>
                    <option value="High wind / exposed height">High wind / exposed height</option>
                    <option value="Restricted site access">Restricted site access</option>
                    <option value="Night / low-visibility work">Night / low-visibility work</option>
                  </select>
                </label>

                <label>
                  Planned work timing / shift
                  <select
                    value={siteContext.work_timing}
                    onChange={(e) => setSiteContext({ ...siteContext, work_timing: e.target.value })}
                  >
                    <option value="Morning shift (08:00 - 16:00)">Morning shift (08:00 - 16:00)</option>
                    <option value="Afternoon / Evening shift (14:00 - 22:00)">Afternoon / Evening shift (14:00 - 22:00)</option>
                    <option value="Night shift (20:00 - 05:00)">Night shift (20:00 - 05:00)</option>
                    <option value="Full day continuous pour">Full day continuous pour</option>
                  </select>
                </label>

                <label className="full-width">
                  Site location (for live weather lookup)
                  <div className="location-input-group">
                    <input
                      value={siteContext.location}
                      onChange={(e) => setSiteContext({ ...siteContext, location: e.target.value })}
                      placeholder="e.g. Pune, Mumbai, Bengaluru..."
                    />
                    <button
                      type="button"
                      className="secondary-button"
                      disabled={assistantWeatherBusy || !siteContext.location.trim()}
                      onClick={fetchAssistantWeather}
                    >
                      {assistantWeatherBusy ? 'Fetching...' : 'Fetch live weather'}
                    </button>
                  </div>
                </label>

                {assistantWeather && (
                  <div className="weather-context-pill full-width">
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span className="live-dot" />
                      <span><strong>Live weather ({assistantWeather.location}):</strong> {assistantWeather.current.condition}, {assistantWeather.current.temperature_c} °C · Precip {assistantWeather.current.precipitation_mm} mm · Wind {assistantWeather.current.wind_speed_kmh} km/h</span>
                    </div>
                    <button type="button" onClick={() => setAssistantWeather(null)} title="Clear weather">✕</button>
                  </div>
                )}

                {assistantWeatherError && (
                  <div className="full-width">
                    <small className="weather-error">{assistantWeatherError}</small>
                  </div>
                )}

                {!assistantWeather && (
                  <label className="full-width">
                    Current or forecast weather (manual description)
                    <input
                      value={siteContext.weather_input}
                      onChange={(e) => setSiteContext({ ...siteContext, weather_input: e.target.value })}
                      placeholder="e.g. Overcast, 28 °C, light rain expected in afternoon..."
                    />
                  </label>
                )}

                <label className="full-width">
                  Site notes and constraints (optional)
                  <input
                    value={siteContext.notes}
                    onChange={(e) => setSiteContext({ ...siteContext, notes: e.target.value })}
                    placeholder="e.g. Concrete pump arrives at 10 AM, formwork inspection pending, wet access ramp..."
                  />
                </label>
              </div>
            </div>

            <div className="prompt-chips">
              <button onClick={() => setQuestion('Review activity suitability, key hazards, and weather precautions for the current site setup.')}>Suitability & precautions</button>
              <button onClick={() => setQuestion('Provide recommended task sequencing and workforce coordination steps for today.')}>Task sequencing</button>
              <button onClick={() => setQuestion('What PPE checks, material handling precautions, and stop-work conditions apply today?')}>Safety & PPE checks</button>
              <button onClick={() => setQuestion('Draft a structured daily site report incorporating current progress, site conditions, and observations.')}>Draft daily report</button>
            </div>

            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="Describe your construction question, plan, or site observations..."
            />

            <div className="assistant-actions">
              <button className="secondary-button" disabled={busy || !question.trim()} onClick={() => ask('daily_report')}>Draft daily report</button>
              <button className="primary-button" disabled={busy || !question.trim()} onClick={() => ask('chat')}>{busy ? 'Thinking...' : 'Ask assistant'}</button>
            </div>

            {reply && (
              <AssistantReport
                reply={reply}
                siteContext={siteContext}
                assistantWeather={assistantWeather}
              />
            )}
          </div>

          <div className="panel safety-note">
            <div className="note-icon">i</div>
            <span className="section-kicker">CONTEXT-AWARE DECISION SUPPORT</span>
            <h2>Keep decisions grounded.</h2>
            <p>ConstructIQ combines your reported site activity, worker count, field conditions, and live weather to deliver actionable recommendations. Confirm safety-critical operations, temporary works design, and regulatory compliance with licensed professionals.</p>
            <div className="note-rule" />
            <b>Live Weather Integration</b>
            <p>Fetch real-time weather directly in the assistant context form or on the Overview dashboard. Live meteorological data informs pour suitability, curing precautions, and wind safety for crane or scaffolding operations.</p>
            <div className="note-rule" />
            <b>Stop-Work Authority</b>
            <p>Any worker or supervisor has the obligation to pause work if weather or physical site conditions pose imminent danger (e.g., lightning, excessive winds above 45 km/h, waterlogging around excavations).</p>
          </div>
        </section>}

        <footer className="app-footer"><span>CONSTRUCTIQ · FIELD INTELLIGENCE</span><span className="footer-links"><a href="https://github.com/aayushsahu0804" target="_blank" rel="noopener noreferrer">GitHub profile</a><a href="https://github.com/aayushsahu0804/ConstructIQ" target="_blank" rel="noopener noreferrer">ConstructIQ repository</a></span></footer>
      </section>
    </main>
  </div>
}

export default App
