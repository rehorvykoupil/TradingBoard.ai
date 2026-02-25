# TradingBoard.ai landing page

Static landing site. Form submissions (early access + contact) are sent **by email only** to **info@tradingboard.ai** using **Google Cloud + Python**: a single Cloud Function sends mail via Gmail SMTP. No database, no third‑party form services.

**→ Step-by-step install and upload: see [DEPLOY.md](DEPLOY.md).**

## 1. Gmail setup (for sending)

1. Use a **Gmail account** (or Google Workspace) that will send the form emails.
2. Turn on **2‑Step Verification** for that account (Google Account → Security).
3. Create an **App Password**: Google Account → Security → 2-Step Verification → App passwords. Generate one for “Mail” and copy the 16‑character password.
4. You’ll pass this account and app password into the Cloud Function as env vars (see below).

## 2. Deploy the Cloud Function (Python)

From this repo (e.g. from `TradingBoard.ai`):

```bash
cd basic_web/cloud_function
gcloud functions deploy sendFormEmail \
  --gen2 \
  --runtime python312 \
  --region us-central1 \
  --trigger-http \
  --allow-unauthenticated \
  --set-env-vars "GMAIL_USER=your-sender@gmail.com,GMAIL_APP_PASSWORD=your-16-char-app-password,TO_EMAIL=info@tradingboard.ai"
```

- Replace `your-sender@gmail.com` with the Gmail address that sends the emails.
- Replace `your-16-char-app-password` with the App Password. Emails are sent to **info@tradingboard.ai** (or set `TO_EMAIL` to another address).
- Ensure project is set: `gcloud config set project my-project-web-488416`

After deploy, note the function URL (e.g. `https://us-central1-my-project-web-488416.cloudfunctions.net/sendFormEmail`).

## 3. Point the site at the function

In `basic_web/index.html`, set `FORM_API_URL` to that URL:

```js
var FORM_API_URL = 'https://us-central1-my-project-web-488416.cloudfunctions.net/sendFormEmail';
```

(Use your actual region and project in the URL.)

## 4. Assets

Put these in `basic_web/assets/` (see `assets/README.md` for sources):

- `logo.png` – navbar logo  
- `ux-screenshot.png` – UX section and lightbox

If they’re missing, the logo falls back to text “TradingBoard.ai”; the screenshot area will be broken until you add the file.

## 5. Run locally

```bash
cd basic_web
npx serve .
# or: python -m http.server 8080
```

Open the URL shown. Submissions will go to your Cloud Function (use the real `FORM_API_URL` to receive emails).

## 6. Deploy the static site

Deploy the contents of `basic_web` (e.g. `index.html`, `assets/`) to any static host:

- **Firebase Hosting**: in `basic_web`, run `firebase init hosting` (public directory: `.`), then `firebase deploy`.
- **Google Cloud Storage**: create a bucket, upload the files, enable static website hosting (or put a load balancer in front).
- **Netlify / Vercel**: point publish directory to `basic_web` or drag the folder.

No database or extra backend — only the Python Cloud Function and Gmail for email.

## Routing

- `#home` – Home  
- `#contact` – Contact (shareable link)

## API (used by the frontend)

Single endpoint: the Cloud Function URL.

- **Early access**: `POST` body `{ "action": "earlyAccess", "email": "..." }`
- **Contact**: `POST` body `{ "action": "contact", "name", "email", "type", "message" }`

Responses: `200 { "success": true }` or `4xx/5xx { "error": "..." }`.
