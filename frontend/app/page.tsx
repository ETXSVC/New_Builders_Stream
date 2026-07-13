import Link from "next/link";
import { Footer, Navigation } from "./components";

const features = [
  ["01", "Know what’s next", "Track every opportunity from the first conversation to a project your team can act on."],
  ["02", "Keep the job connected", "Bring tasks, phases, documents, and daily progress into the same shared view."],
  ["03", "Turn clarity into momentum", "Move estimates toward approval and give every role the information it needs."],
];

export default function Home() {
  return <><Navigation /><main>
    <section className="hero"><div className="shell hero-grid"><div className="hero-copy"><p className="eyebrow light">FOR TEAMS THAT BUILD</p><h1>Construction work,<br /><em>in one clear flow.</em></h1><p>Builders Stream connects your leads, projects, documents, estimates, and field updates—so your team can spend less time chasing information and more time moving work forward.</p><div className="hero-actions"><Link className="button" href="/early-access">Request early access <span>→</span></Link><Link className="text-link light" href="/solutions">Explore the workflow <span>↓</span></Link></div></div><div className="hero-visual"><div className="sun"></div><div className="house house-back"><i></i><i></i></div><div className="house house-front"><i></i><i></i><i></i></div><div className="site-card"><span className="pulse"></span><b>Project on track</b><small>12 tasks completed this week</small></div><div className="hero-line"></div></div></div></section>
    <section className="intro shell"><p className="eyebrow">ONE PLACE. EVERY HANDOFF.</p><div className="split-title"><h2>The work isn’t scattered.<br /><em>Why should your information be?</em></h2><p>Growing construction companies outgrow spreadsheets, group texts, and disconnected tools quickly. Builders Stream gives the office and the jobsite one dependable source of truth.</p></div></section>
    <section className="feature-section"><div className="shell"><div className="feature-top"><p className="eyebrow">THE BUILDERS STREAM WAY</p><p>Built around the work your team already does.</p></div><div className="feature-grid">{features.map(([number, title, copy]) => <article key={number} className="feature"><span>{number}</span><h3>{title}</h3><p>{copy}</p><div className="arrow">↗</div></article>)}</div></div></section>
    <section className="workflow shell"><div className="workflow-copy"><p className="eyebrow">A CONNECTED WORKFLOW</p><h2>From first lead<br />to <em>finished work.</em></h2><p>Every step keeps the next one moving. Capture the details, create the plan, keep the team in sync, and give customers confidence in what’s happening.</p><Link className="text-link" href="/solutions">See how it works <span>→</span></Link></div><div className="workflow-art"><img src="/images/connected-workflow.png" alt="Builders Stream connected workflow diagram" /><div className="art-label">One connected system<br /><b>for the whole job</b></div></div></section>
    <section className="role-band"><div className="shell"><p className="eyebrow light">BUILT FOR THE WHOLE TEAM</p><div className="roles"><span>Office</span><span>Project managers</span><span>Field crews</span><span>Clients</span></div></div></section>
    <section className="cta shell"><p className="eyebrow">READY FOR A CLEARER VIEW?</p><h2>Bring the whole job<br /><em>into view.</em></h2><p>Join the early-access list and help shape a better way to run construction work.</p><Link className="button" href="/early-access">Request early access <span>→</span></Link></section>
  </main><Footer /></>;
}
