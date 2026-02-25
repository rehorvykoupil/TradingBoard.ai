# TradingBoard.ai Landing Page – Analysis & Implementation Plan

**Template:** `spec/UI-templates/Home_page_65.html`  
**Target:** `basic_web/` directory  
**Deployment:** Google Cloud, project ID `my-project-web-488416`

---

## 1. Template Analysis Summary

### 1.1 Structure
- **Single-page app (SPA) style:** Two "views" (Home, Contact) switched via `showPage('home')` / `showPage('contact')` with no URL routing.
- **Tech stack:** Tailwind CSS (CDN), Three.js (CDN), Lucide icons (CDN), Google Fonts (Inter).
- **Features:** Fixed starfield background (Three.js), two 3D cube + bouncing-ball canvases (hero + contact), scroll reveal (IntersectionObserver), lightbox for UX screenshot, email CTA inputs, contact form.
- **Assets:** Logo and UX screenshot loaded from Google Drive (`drive.google.com/thumbnail?id=...`).

### 1.2 What Works Out of the Box
- Layout, typography, and Tailwind styling.
- Three.js starfield and cube/ball animations.
- Page switching (Home ↔ Contact).
- Scroll reveal for `.reveal` sections.
- Lightbox open/close (structure is there).
- Responsive layout and resize handling for canvases.

### 1.3 Issues to Fix for "Fully Functional"

| # | Issue | Impact | Fix |
|---|--------|--------|-----|
| 1 | **Lucide icons not loading** | All `<i data-lucide="...">` icons are empty. | Script src is wrong: `https://unpkg.com/lucide@latest` does not point to a JS file. Use `https://unpkg.com/lucide@latest/dist/umd/lucide.min.js` (or pin a version). Call `lucide.createIcons()` after DOM ready; re-run when switching to Contact. |
| 2 | **Lightbox image URL** | Google Drive thumbnail URL may not be a direct image; lightbox might show HTML. | Use a direct image URL (host screenshot in Cloud Storage or in `assets/`) or test and replace if needed. |
| 3 | **CTA & Contact form non-functional** | "Request Early Access" and "Send Message" do nothing. | **Option A:** mailto or client-side "Thank you" only. **Option B:** Cloud Function (or Firebase Function) for POST; wire form/CTA to it. |
| 4 | **Google Drive assets** | Logo/screenshot can break with permissions/CORS. | Keep as-is and document, or host in `basic_web/assets/` or GCP bucket. |
| 5 | **Cube container when Contact first shown** | Hidden view => container 0x0 => Three.js wrong size. | On `showPage('contact')`, trigger resize for cube containers. |
| 6 | **CSS variable in hover** | `rgba(var(--tb-blue), 0.5)` invalid; `--tb-blue` is rgb(). | Use `--tb-blue-rgb: 24, 97, 217` and `rgba(var(--tb-blue-rgb), 0.5)`. |
| 7 | **Footer year** | Hardcoded "© 2024". | Optional: set via JS. |
| 8 | **No deployment config** | No GCP config in repo. | Add Firebase Hosting (or Cloud Storage + LB) and, if needed, Cloud Function. |

---

## 2. Clarifications / Decisions Needed

1. **Form/CTA backend**  
   Static only (mailto / thank-you message) or real submission (email/Firestore via Cloud Function)?  
   → **Decided: real backend.**

2. **Assets**  
   Keep Google Drive links or host logo/screenshot in repo (`basic_web/assets/`) or GCP bucket?  
   → **Decided: download and host here (in `basic_web/assets/`).**

3. **Deployment**  
   Prefer **Firebase Hosting** (easiest) or **Cloud Storage + load balancer**?  
   → See **§2a** below for the difference.

4. **URL routing**  
   Add hash/path URLs (e.g. `/#contact`, `/contact`) so Contact is shareable?  
   → See **§2b** below for the difference.

---

### 2a. Deployment: Firebase Hosting vs Cloud Storage + Load Balancer

| | **Firebase Hosting** | **Cloud Storage + Load Balancer** |
|---|----------------------|-----------------------------------|
| **What it is** | Managed static hosting; you run `firebase deploy` and get a URL (e.g. `my-project-web-488416.web.app`). | You create a GCS bucket, upload files, then attach a load balancer with a backend bucket and (optionally) Cloud CDN. |
| **Setup** | `firebase init` → choose Hosting → point to `basic_web`. Few minutes. | More steps: enable APIs, create bucket, set permissions, create HTTP(S) load balancer, SSL cert (e.g. Google-managed), DNS. |
| **Cost** | Generous free tier (10 GB storage, 360 MB/day transfer). Good for small/medium sites. | You pay for storage, egress, and load balancer. Better when you need strict control, custom domains at the bucket level, or very high traffic. |
| **Custom domain** | Supported (connect domain in Firebase console). | Supported (via load balancer and DNS). |
| **Best for** | Simple static sites, fast iteration, single command deploy. | When you already use GCS, need CDN/load balancer for other services, or want everything in one GCP project without Firebase. |

**Recommendation:** For this landing page, **Firebase Hosting** is simpler and usually cheaper unless you have a specific reason to use a bucket + load balancer.

---

### 2b. URL routing: No routing vs hash/path URLs

| | **No routing (current)** | **Hash or path URLs** |
|---|---------------------------|------------------------|
| **Behaviour** | Only one URL (e.g. `https://yoursite.web.app/`). Clicking "Contact" only changes the visible view with JS; the address bar stays the same. | Contact has its own URL: `https://yoursite.web.app/#contact` or `https://yoursite.web.app/contact`. |
| **Sharing** | You can’t send a link that opens directly on Contact. | You can send `yoursite.web.app/#contact` or `yoursite.web.app/contact` and the page opens on Contact. |
| **Refresh** | Refreshing always shows Home. | With routing, refreshing on `/contact` keeps Contact. |
| **Implementation** | Nothing to add. | Hash: read `location.hash`, set it when switching views, handle `hashchange`. Path: needs server or Firebase rewrites so `/contact` serves `index.html`; then JS reads `location.pathname` and shows the right view. |

**Recommendation:** Adding **hash routing** (`/#contact`) is small code and doesn’t require server config. **Path routing** (`/contact`) is nicer for SEO and sharing but needs Firebase (or server) rewrites so all paths serve `index.html`.

---

## 3. Implementation Plan (After Decisions)

### Phase 1 – Repo and static site
1. Create `basic_web/` and add `index.html` from template (with fixes).
2. Fix Lucide script URL; run `lucide.createIcons()` on load and after `showPage('contact')`.
3. Fix `.ux-screenshot-box:hover` CSS (rgba).
4. Optional: `basic_web/assets/` for logo/screenshot; update lightbox URL.
5. On `showPage()`, trigger Three.js resize for cube containers.
6. Optional: dynamic footer year.

### Phase 2 – Forms and CTA
- Static: form submit handler + thank-you (and optional mailto).
- With backend: Cloud Function for POST; wire form/CTA with validation and loading state.

### Phase 3 – Deployment (GCP)
- Firebase: `firebase.json`, `.firebaserc` (project `my-project-web-488416`), deploy from `basic_web`.
- Or: Cloud Storage bucket + load balancer docs/script.

### Phase 4 – Optional
- ~~URL routing for Home/Contact.~~ **Done:** hash routing (`#home`, `#contact`).
- Meta tags, favicon.
- ~~`basic_web/README.md` with run + deploy instructions.~~ **Done.**

---

## 4. Proposed File Layout

```
TradingBoard.ai/
  basic_web/               # Self-contained landing site (no mixing with real app)
    index.html
    assets/
    firebase.json
    .firebaserc
    functions/
      index.js
      package.json
    PLAN.md
    README.md
```
