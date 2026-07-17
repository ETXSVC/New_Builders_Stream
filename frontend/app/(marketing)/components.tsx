import Link from "next/link";

export function Mark() {
  return <Link className="mark" href="/"><span className="mark-icon">B</span><span>Builders<br /><b>Stream</b></span></Link>;
}

export function Navigation() {
  return <header className="nav-wrap"><nav className="nav shell"><Mark /><div className="nav-links"><Link href="/solutions">Solutions</Link><Link href="/security">Security</Link><Link href="/about">About</Link></div><Link className="button button-small" href="/early-access">Request early access <span>→</span></Link></nav></header>;
}

export function Footer() {
  return <footer><div className="shell footer-grid"><div><Mark /><p>Construction work, in one clear flow.</p></div><div><p className="eyebrow">Explore</p><Link href="/solutions">Solutions</Link><Link href="/security">Security</Link><Link href="/about">About us</Link></div><div><p className="eyebrow">Get started</p><Link href="/early-access">Request early access</Link><a href="mailto:hello@buildersstream.com">hello@buildersstream.com</a></div></div><div className="shell footer-bottom">© 2026 Builders Stream. Built for the people building what’s next.</div></footer>;
}

export function PageHero({ eyebrow, title, copy }: { eyebrow: string; title: string; copy: string }) {
  return <section className="page-hero"><div className="shell"><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p className="lede">{copy}</p></div></section>;
}
