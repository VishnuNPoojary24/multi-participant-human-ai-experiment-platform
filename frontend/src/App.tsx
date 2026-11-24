import { FormEvent, useEffect, useRef, useState } from 'react';

type Participant = { id: string };
type Message = { id: number; round: number; participant_id: string; content: string; created_at: string };
type State = { type: string; session_id: string; status: string; round: number; round_duration_seconds: number; participants: Participant[]; messages: Message[]; voted?: string[] };
type Results = { votes: Record<string, number>; winner: string; strategy: string };
const API = import.meta.env.VITE_API_URL || '';

export default function App() {
  const [sessionId, setSessionId] = useState(localStorage.getItem('sessionId') || '');
  const [participantId, setParticipantId] = useState(localStorage.getItem('participantId') || '');
  const [state, setState] = useState<State | null>(null);
  const [results, setResults] = useState<Results | null>(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [choice, setChoice] = useState('');
  const [streams, setStreams] = useState<Record<string, { round: number; participant_id: string; content: string }>>({});
  const [seconds, setSeconds] = useState(60);
  const [sessionIdCopied, setSessionIdCopied] = useState(false);
  const socket = useRef<WebSocket | null>(null);
  const resultsRef = useRef<HTMLElement | null>(null);
  const transcriptRef = useRef<HTMLDivElement | null>(null);

  const adopt = (value: State) => { setState(value); setSessionId(value.session_id); localStorage.setItem('sessionId', value.session_id); };
  const connect = (sid: string, pid: string) => {
    socket.current?.close();
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const apiBase = API || `${location.protocol}//${location.host}`;
    const wsBase = apiBase.replace(/^http/, 'ws');
    const ws = new WebSocket(`${wsBase}/ws/${sid}/${pid}`);
    socket.current = ws;
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'state') {
        adopt(data);
        if (data.status === 'complete') {
          fetch(`${API}/api/sessions/${sid}/results`)
            .then(async res => { const result = await res.json(); if (!res.ok) throw Error(result.detail); setResults(result); })
            .catch(e => setError(String(e)));
        }
      }
      else if (data.type === 'results') setResults(data);
      else if (data.type === 'message') {
        setStreams(old => { const next = { ...old }; delete next[`${data.message.round}-${data.message.participant_id}`]; return next; });
        setState(old => old ? { ...old, messages: [...old.messages, data.message] } : old);
      }
      else if (data.type === 'ai_message_delta') {
        const key = `${data.round}-${data.participant_id}`;
        setStreams(old => ({ ...old, [key]: { round: data.round, participant_id: data.participant_id, content: (old[key]?.content || '') + data.content } }));
      }
      else if (data.type === 'ai_message_reset') setStreams(old => { const next = { ...old }; delete next[`${data.round}-${data.participant_id}`]; return next; });
      else if (data.type === 'participant_joined') setState(old => old ? { ...old, participants: data.participants } : old);
      else if (['experiment_started', 'round_started'].includes(data.type)) setState(old => old ? { ...old, status: 'discussion', round: data.round } : old);
      else if (data.type === 'round_ended') setState(old => old ? { ...old, status: 'voting', round: data.round } : old);
      else if (data.type === 'vote_update') setState(old => old ? { ...old, status: 'voting' } : old);
      else if (data.type === 'error') setError(data.message);
    };
    ws.onclose = () => {};
  };

  useEffect(() => {
    if (sessionId && participantId) connect(sessionId, participantId);
    return () => socket.current?.close();
  }, []);
  useEffect(() => {
    if (state?.status !== 'discussion') return;
    setSeconds(state.round_duration_seconds || 60);
    const timer = window.setInterval(() => setSeconds(s => Math.max(0, s - 1)), 1000);
    return () => clearInterval(timer);
  }, [state?.round, state?.status]);
  useEffect(() => { setChoice(''); }, [state?.round]);
  useEffect(() => {
    if (!results) return;
    const frame = requestAnimationFrame(() => resultsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
    return () => cancelAnimationFrame(frame);
  }, [results]);
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const transcript = transcriptRef.current;
      if (transcript) transcript.scrollTop = transcript.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [state?.messages, state?.round, state?.status, streams]);

  async function createSession() {
    try { const res = await fetch(`${API}/api/sessions`, { method: 'POST' }); const data = await res.json(); if (!res.ok) throw Error(data.detail); setParticipantId(''); localStorage.removeItem('participantId'); adopt(data); setError('Session created. Join to take a participant slot.'); }
    catch (e) { setError(String(e)); }
  }
  async function join(e: FormEvent) {
    e.preventDefault(); setError('');
    const fullSessionId = sessionId.trim();
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(fullSessionId)) {
      setError('Enter the full 36-character session ID. The short code is not enough to join.');
      return;
    }
    try {
      const humanKey = localStorage.getItem('humanKey') || crypto.randomUUID(); localStorage.setItem('humanKey', humanKey);
      const res = await fetch(`${API}/api/sessions/${fullSessionId}/join`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ human_key: humanKey }) });
      const data = await res.json(); if (!res.ok) throw Error(data.detail);
      setParticipantId(data.participant_id); localStorage.setItem('participantId', data.participant_id); adopt(data.state); connect(fullSessionId, data.participant_id);
    } catch (e) { setError(String(e)); }
  }
  async function copySessionId() {
    try {
      await navigator.clipboard.writeText(sessionId);
      setSessionIdCopied(true);
      window.setTimeout(() => setSessionIdCopied(false), 2000);
    } catch {
      setError(`Copy this full session ID: ${sessionId}`);
    }
  }
  function sendMessage(e: FormEvent) { e.preventDefault(); if (message.trim()) { socket.current?.send(JSON.stringify({ type: 'message', content: message.trim() })); setMessage(''); } }
  async function submitVote() {
    try {
      const res = await fetch(`${API}/api/sessions/${sessionId}/votes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ participant_id: participantId, candidate_id: choice }) });
      const data = await res.json(); if (!res.ok) throw Error(data.detail); setError('Vote submitted. Waiting for the other participants...');
    } catch (e) { setError(String(e)); }
  }
  async function refresh() {
    try { const res = await fetch(`${API}/api/sessions/${sessionId}`); const data = await res.json(); if (!res.ok) throw Error(data.detail); adopt(data); if (data.status === 'complete') { const r = await fetch(`${API}/api/sessions/${sessionId}/results`); setResults(await r.json()); } }
    catch (e) { setError(String(e)); }
  }
  function returnHome() {
    socket.current?.close();
    socket.current = null;
    localStorage.removeItem('sessionId');
    localStorage.removeItem('participantId');
    localStorage.removeItem('humanKey');
    window.history.replaceState({}, '', '/');
    setSessionId('');
    setParticipantId('');
    setState(null);
    setResults(null);
    setMessage('');
    setChoice('');
    setStreams({});
    setError('');
    setSessionIdCopied(false);
  }

  return <main className="shell">
    <header><div className="mark">CHM <span>RESEARCH STUDY</span></div><div className="live"><i /> PRIVATE SESSION</div></header>
    <section className="hero"><div className="eyebrow">GROUP DECISION-MAKING | BERLIN</div><h1>One group.<br/><em>Three perspectives.</em></h1><p>Discuss who should be elected Decision Maker for a later experiment. Take time to listen, share your reasoning, and decide together.</p></section>
    {!state && <section className="panel entry"><h2>Enter the study</h2><p>Start a new session or join one using its session ID. Your participant identity is shown as a neutral player label.</p>
      <button onClick={createSession} className="primary">Create a session</button>
      <form onSubmit={join} className="join"><label>Full session ID<input required maxLength={36} value={sessionId} onChange={e => setSessionId(e.target.value)} placeholder="Paste the full 36-character ID"/></label><button className="secondary">Join session</button></form>
      {sessionId && !participantId && <small>New session: share this ID with the other participants.</small>}
    </section>}
    {state && <>
      <div className="sessionbar"><span>SESSION <b className="session-id">{sessionId}</b></span><div><button onClick={copySessionId}>{sessionIdCopied ? 'Copied' : 'Copy full ID'}</button><button onClick={refresh}>Refresh state</button></div></div>
      <section className="panel roster"><div className="section-head"><div><div className="eyebrow">THE GROUP</div><h2>Participants</h2></div><span className="count">{state.status === 'waiting' ? 'GROUP FORMING' : '3 PARTICIPANTS'}</span></div>
        <div className="players">{(state.participants || []).map(p => <div className="player" key={p.id}><span className="avatar">{p.id}</span><div><b>Player {p.id}</b><small>{state.status === 'waiting' ? 'Participant seat' : 'In this session'}</small></div><i className={state.status === 'waiting' ? '' : 'present'}/></div>)}</div>
      </section>
      {state.status === 'waiting' && <section className="panel waiting"><div className="pulse"/><div><h2>Waiting for the group</h2><p>The discussion begins when the group is ready. Share the full session ID above with the other participants.</p><button type="button" className="secondary" onClick={copySessionId}>{sessionIdCopied ? 'Copied' : 'Copy full session ID'}</button>{!participantId && <form onSubmit={join}><button className="primary">Join the session</button></form>}</div></section>}
      {state.status === 'discussion' && <section className="panel room"><div className="section-head"><div><div className="eyebrow">DISCUSSION | ROUND {state.round} OF 2</div><h2>Make your case</h2></div><div className="clock">TIME {String(Math.floor(seconds/60)).padStart(2,'0')}:{String(seconds%60).padStart(2,'0')}</div></div>
        <div className="transcript" ref={transcriptRef}>{state.messages.filter(m => m.round === state.round).map(m => <article className="chat" key={m.id}><span className="avatar small">{m.participant_id}</span><div><div className="chathead"><b>Player {m.participant_id}</b><time>{new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time></div><p>{m.content}</p></div></article>)}{Object.entries(streams).filter(([, item]) => item.round === state.round).map(([key, item]) => <article className="chat" key={`stream-${key}`}><span className="avatar small">{item.participant_id}</span><div><div className="chathead"><b>Player {item.participant_id}</b><time>Responding...</time></div><p>{item.content}</p></div></article>)}</div>
        <form className="composer" onSubmit={sendMessage}><input value={message} onChange={e => setMessage(e.target.value)} placeholder={`Write as Player ${participantId || "..."}`} maxLength={1000}/><button className="primary" disabled={!participantId}>Send</button></form>
      </section>}
      {state.status === 'voting' && <section className="panel voting"><div className="eyebrow">ROUND {state.round} | PRIVATE BALLOT</div><h2>Who should be Decision Maker?</h2><p>Choose one of the three participants. Your vote is private until the study is complete.</p><div className="choices">{state.participants.map(p => <button key={p.id} onClick={() => setChoice(p.id)} className={choice === p.id ? 'selected' : ''}><span className="avatar">{p.id}</span><b>Player {p.id}</b>{p.id === participantId && <small>Your seat</small>}</button>)}</div><button className="primary" disabled={!choice} onClick={submitVote}>Confirm vote</button></section>}
      {results && <section className="panel result" ref={resultsRef} tabIndex={-1} aria-label="Final experiment results"><div className="eyebrow">STUDY COMPLETE</div><h2>Decision Maker elected</h2><div className="winner-card"><span className="winner-tag">DECISION MAKER</span><div className="winner">Player {results.winner}</div></div><div className="totals">{Object.entries(results.votes).map(([id, total]) => <div key={id}><span>Player {id}</span><b>{total} {total === 1 ? 'vote' : 'votes'}</b></div>)}</div><div className="result-actions"><button className="primary" onClick={returnHome}>Back to homepage</button></div></section>}
    </>}
    {error && <div className="notice" role="status">{error}<button onClick={() => setError('')}>x</button></div>}
    <footer><span>HUMAN-AI DECISION STUDY</span><span>All participant identities are presented neutrally.</span></footer>
  </main>;
}
