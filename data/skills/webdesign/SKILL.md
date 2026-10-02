---
name: webdesign
description: HTML-native 高保真网页设计技能：落地页/演示稿/仪表盘/信息图/App 原型用纯 HTML/CSS/JS 实现，无框架无构建，产出前先事实核验与灰盒迭代
---

# Huashu Web Design

HTML-native design skill for AI coding agents. Produces high-fidelity pages, landing pages, presentations, dashboards, infographics, and app prototypes using pure HTML/CSS/JS — no framework, no build step, no JSX.

Adapted from alchaincyf/huashu-design for vanilla web.

---

## Priority #0 — Fact Verification

Before writing a single line of HTML, if the task references a product, company, person, technology, event, or date, **you must verify it via web search first**. Never rely on training data for:

- Whether a product exists or has shipped
- Current version numbers, pricing, feature sets
- Names, roles, founding dates of companies/people
- Statistics, market sizes, user counts

If you cannot verify, say so explicitly and mark the claim as unverified in a comment. Designing for a product that doesn't exist is the single most expensive mistake this skill prevents.

---

## Workflow Modes

### Default: Junior Designer Mode

Work like a thoughtful junior designer. Show assumptions early. Iterate visibly.

1. **Before writing**: Ask 3-5 clarifying questions if the brief is ambiguous. Batch them. Wait for answers.
2. **First draft**: Gray-box layout only. Correct structure, wrong aesthetics. Show this.
3. **Second pass**: Real content, real colors, real type. Show this.
4. **Third pass**: Variations, micro-interactions, polish. Show this.
5. **Delivery**: One final pass with responsive check.

Never skip steps. Never deliver a "finished" design without showing intermediate work. If the user didn't react to gray boxes, you might be building the wrong thing.

### Vague Brief Mode: Design Direction Advisor

Triggered when the user says things like "make something nice", "modern look", "help me design" without concrete direction.

**Do this:**
1. Propose 3 differentiated directions from **different schools** (below).
2. For each direction, write 2-3 sentences explaining: visual language, emotional tone, who it suits.
3. For each direction, produce a minimal 300px-tall demo block so the user can see the vibe immediately.
4. Let the user pick one. Do not pick for them.

Rules for the 3 directions:
- Must come from 3 different schools
- Must be genuinely different in feel, not three flavors of the same taste
- Must include at least one bold/experimental option

---

## Brand Asset Protocol

When the task involves a specific brand (Stripe, Linear, Notion, Vercel, or any company), follow this 5-step protocol. **Do not guess brand colors from memory.**

**Step 1 — Ask.** Does the user have brand guidelines, a logo file, a style guide URL? If yes, use those.

**Step 2 — Search.** Visit `<brand>.com`, `<brand>.com/brand`, `<brand>.com/press`, or `<brand>.<tld>.com`. Look for press kits, brand pages, logo libraries.

**Step 3 — Download.** Grab SVG logos first. If no SVG, grab a high-res PNG. If neither, screenshot the homepage hero.

**Step 4 — Extract.** Grep all `#xxxxxx` hex values from the brand's CSS/HTML. Sort by frequency. Filter out near-black, near-white, and grays. What remains are the brand colors.

**Step 5 — Solidify.** Write a `brand-spec.md` in the working directory:
```
## Brand: <name>
- Primary: #xxxxxx (from <source URL>)
- Secondary: #xxxxxx
- Accent: #xxxxxx
- Background: #xxxxxx
- Text: #xxxxxx
- Fonts: <font stack>
- Logo: <path or URL>
```
All subsequent HTML must reference these via CSS variables. Never hardcode hex in component styles.

---

## The 20 Design Philosophies

When the user gives vague direction, or when choosing a style, select from these 20 philosophies across 5 schools. Each philosophy has a name, a visual DNA, and an emotional register.

### School 1 — Information Architecture
*Data as building material, not decoration.*

**01 Pentagram / Michael Bierut** — Typography as primary language. Swiss grid. Black/white + one accent color. 60%+ whitespace. Serif display + grotesque body. Feels: editorial, authoritative, considered.

**02 Stamen Design** — Cartographic data viz. Warm organic palette (terracotta, sage, deep blue). Layered, textured, topographic. Feels: earthy, scientific, human.

**03 Information Architects** — Content-first hierarchy. Zero decoration. System fonts only. Classic blue hyperlinks. Optimal reading line length (65-75ch). Feels: utilitarian, honest, legible.

**04 Fathom** — Data as physical sculpture. Interactive 3D-feeling visualizations. Scientific precision. Monospaced data. Feels: rigorous, immersive, precise.

### School 2 — Kinetic Poetry
*Motion as meaning, not ornament.*

**05 Locomotive** — Scroll choreography. Page transitions as narrative. Cinematic pacing. Eased, deliberate motion. Feels: dramatic, story-driven, premium.

**06 Active Theory** — Generative motion. Real-time particles, canvas/WebGL. Interactive, responsive, alive. Feels: playful, alive, experimental.

**07 Field.io** — Kinetic typography. Dynamic letterforms. Experimental type animation. Bold. Feels: expressive, rhythmic, loud.

**08 Resn** — Playful interaction design. Unexpected micro-interactions. Surprises. Feels: joyful, cheeky, human.

### School 3 — Minimalist Order
*Restraint as the highest skill.*

**09 Experimental Jetset** — Reductive. Strip to essentials. Anti-decorative. Content is the only visual. Feels: confident, sparse, essential.

**10 Müller-Brockmann** — Swiss grid mathematics. Precise geometric spacing. Objective photography. Feels: rational, timeless, architectural.

**11 Build** — Crafted modernism. Material honesty. Refined typography. Subtle texture. Feels: premium, considered, tactile.

**12 Sagmeister & Walsh** — Emotional provocation. Experimental typography. Raw, unconventional. Feels: bold, confrontational, memorable.

### School 4 — Experimental Vanguard
*Breaking rules with purpose.*

**13 Zach Lieberman** — Creative coding. Playful algorithms. Generative art. Interactive experiments. Feels: whimsical, curious, alive.

**14 Raven Kwok** — Algorithmic art. Rule-based systems. Computational aesthetics. Mathematical beauty. Feels: precise, mystical, abstract.

**15 Ash Thorp** — Cinematic futures. HUD design. Sci-fi UI. Atmospheric depth. Feels: dramatic, immersive, cinematic.

**16 Territory Studio** — Screen fiction. Film UI systems. Diegetic interfaces. Narrative technology. Feels: fictional, immersive, world-building.

### School 5 — Eastern Philosophy
*Emptiness as presence.*

**17 Takram** — Japanese speculative design. Elegant concept prototypes. Soft tech. Modest sophistication. Feels: quiet, futuristic, humane.

**18 Kenya Hara** — Emptiness design. 80%+ whitespace. Paper texture in digital form. Layers of white. Feels: meditative, refined, spacious.

**19 Irma Boom** — Book architecture. Non-linear information. Unexpected color. Editorial design. Feels: surprising, structural, intellectual.

**20 Neo Shen** — Contemporary Eastern aesthetic. Digital ink wash. Soft glow. Poetic negative space. Feels: poetic, tranquil, meditative.

---

## Anti AI-Slop Rules

These patterns make AI-generated designs instantly recognizable and cheap-looking. **Never do these:**

**Visual patterns — banned:**
- Purple-to-blue gradient as default background
- Emoji used as icons or UI elements
- Rounded rectangle with a colored left border accent (the "AI card")
- SVG-drawn cartoon faces or avatars
- Inter font as display typeface
- Glassmorphism with `backdrop-filter: blur(20px)` on everything
- "Modern SaaS" pastel palette with gradient text
- Icon-only nav with no labels
- Centered hero with H1 + subtitle + two buttons (one filled, one outline)
- Three-column "feature" grid as the default layout
- Fake "stats" numbers (e.g., "99.9% uptime") without source

**Typography rules:**
- Use `text-wrap: pretty` or `balance` on headings
- Display size: 3rem+ minimum. Never 1.5rem "hero"
- Line length: 50-75ch for body, shorter for headings
- Use serif display fonts (`Fraunces`, `Instrument Serif`, `GT Sectra`, `Reco`) or geometric sans (`Sohne`, `Neue Haas Grotesk`) — not Inter, not system default

**Color rules:**
- Prefer `oklch()` or `hsl()` for perceptual consistency
- Maximum 3 distinct hues in a palette
- Test contrast: 4.5:1 minimum for body text, 3:1 for large text
- Avoid pure black `#000` on pure white `#fff` — use `#111` on `#fafafa` instead

**Layout rules:**
- Use CSS Grid, not flexbox-for-everything
- Vary section heights — not all equal
- Asymmetry > center-alignment
- White space is not "empty" — it is the composition

**Content rules:**
- Never use "Lorem ipsum" — use realistic copy, even if invented
- Never invent statistics — if you don't have a source, don't fabricate
- Never use "Lorem" company names (Acme, Corp, Inc) — invent real-sounding ones

---

## Technical Guidelines (Vanilla HTML/CSS/JS)

### No frameworks required. No build step.

Output a single `.html` file that runs by double-clicking. All CSS in `<style>` in `<head>`, all JS in `<script>` at end of `<body>`. Inline SVG for icons. Google Fonts via `<link>`.

### Device frames (for app prototypes)

To show mobile/app UI, wrap in a device bezel:

```css
.iphone-frame {
  width: 375px; height: 812px;
  border: 12px solid #1a1a1a;
  border-radius: 50px;
  box-shadow: 0 25px 80px rgba(0,0,0,.3);
  position: relative; overflow: hidden;
}
.iphone-frame::before {
  content: ''; position: absolute; top: 14px; left: 50%;
  transform: translateX(-50%);
  width: 120px; height: 30px;
  background: #1a1a1a; border-radius: 16px; z-index: 999;
}
```

### Motion (prefer CSS over JS)

```css
/* Fade-up on scroll */
.reveal { opacity: 0; transform: translateY(24px); transition: all .8s cubic-bezier(.2,.7,.2,1); }
.reveal.in { opacity: 1; transform: none; }
```

```js
/* Intersection observer — 8 lines total */
const obs = new IntersectionObserver(entries => {
  entries.forEach(e => { if (e.isIntersecting) e.target.classList.add('in'); });
}, { threshold: 0.15 });
document.querySelectorAll('.reveal').forEach(el => obs.observe(el));
```

For complex animations (particles, generative art, scroll-driven storytelling), use GSAP (CDN) or plain Canvas 2D. Avoid heavy WebGL unless explicitly requested.

### Interactive prototypes

Clickable multi-screen prototypes: use a single page with screen sections toggled by JS. Each "screen" is a `<section>` with `display: none` until activated.

```js
function goTo(screenId) {
  document.querySelectorAll('[data-screen]').forEach(s => s.style.display = 'none');
  document.querySelector(`[data-screen="${screenId}"]`).style.display = 'block';
  history.pushState(null, '', '#' + screenId);
}
```

### Slides / presentations

Single-file HTML deck. Each slide is a `<section>` 100vw × 100vh. Navigate with arrow keys, clickable prev/next buttons, or a progress bar. Slide index shown in bottom corner. Full-screen button (`requestFullscreen`) in the corner.

### Export options (if user asks)

- **PNG/PDF**: user opens browser, prints to PDF, or uses any screenshot tool. Don't build custom export.
- **Video**: recommend Loom or OBS. If user needs it built, use `html2canvas` + `MediaRecorder` API (works, but heavy).
- **PPTX**: do not attempt. Recommend user copy-paste screenshots into PowerPoint.

---

## Output Channels

| Deliverable | Format | Time |
|---|---|---|
| Landing page | Single HTML | 5-10 min |
| Multi-page site | HTML + CSS + JS files | 15-25 min |
| App prototype | HTML with device frame | 10-15 min |
| Presentation deck | Single HTML, arrow-nav | 15-25 min |
| Dashboard | HTML + Chart.js | 10-15 min |
| Infographic | HTML, print-optimized | 10 min |
| Animation/interactive | HTML + Canvas/GSAP | 8-12 min |

---

## Quality Checklist (before delivery)

- [ ] Fact-checked all external claims
- [ ] No AI-slop patterns present
- [ ] Responsive at 375px, 768px, 1440px
- [ ] Type contrast ≥ 4.5:1
- [ ] Motion respects `prefers-reduced-motion`
- [ ] All interactive elements work (test in browser)
- [ ] No `TODO`, `FIXME`, or placeholder text left
- [ ] Copy is real, not Lorem ipsum
- [ ] Single file runs without errors (no console warnings)
- [ ] Shows intermediate work if multi-step

---

## When to refuse or redirect

- **Needs real Figma-level vector export** → tell the user this is out of scope; recommend Figma
- **Needs custom 3D/physics** → recommend Three.js or a dedicated tool; don't fake it in CSS
- **Needs real backend** → this skill is front-end only; suggest Next.js/Express if needed
- **Needs production deployment** → recommend Vercel/Netlify; this skill produces files, not hosted sites

---

## License

Adapted from alchaincyf/huashu-design (MIT License) by 竹瑶. Original: https://github.com/alchaincyf/huashu-design

Free for personal and commercial use. No attribution required but appreciated.
