# Deploy to Google Cloud — step by step

Form submissions are sent to **info@tradingboard.ai** via a Python Cloud Function. The static site can be hosted on Firebase Hosting (same Google project).

---

## Part A: Install Google Cloud SDK

### Windows

1. **Download the installer**  
   https://cloud.google.com/sdk/docs/install  
   Choose “Windows” and run the installer (e.g. `GoogleCloudSDKInstaller.exe`).

2. **Run the installer**  
   - Use default options (install for current user is fine).  
   - At the end, optionally run “Run gcloud init” to log in and pick a project.

3. **Open a new terminal** (PowerShell or Command Prompt) and check:
   ```powershell
   gcloud --version
   ```
   You should see the gcloud version and Python.

### macOS / Linux

- **macOS**:  
  `brew install --cask google-cloud-sdk`  
  or use the install script from https://cloud.google.com/sdk/docs/install.
- **Linux**: use the same install page and choose your distro (e.g. install script or package).

Then in a new terminal:
```bash
gcloud --version
```

---

## Part B: Log in and set project

Run once (or when you need to switch account/project):

```bash
gcloud auth login
```

A browser opens; sign in with the Google account that has access to project **my-project-web-488416**.

Set the default project:

```bash
gcloud config set project my-project-web-488416
```

Check:

```bash
gcloud config list
```

You should see `project = my-project-web-488416`.

---

## Part C: Gmail for sending (one-time)

The Cloud Function sends email via Gmail. You need:

1. A **Gmail address** (or Google Workspace) that will send the form emails (e.g. info@tradingboard.ai if it’s on Google Workspace, or a personal Gmail).
2. **2-Step Verification** turned on for that account (Google Account → Security).
3. An **App Password**: Google Account → Security → 2-Step Verification → App passwords → generate one for “Mail”. Copy the 16-character password (spaces don’t matter).

You’ll use that address and app password in the next step.

---

## Part D: Deploy the Cloud Function (emails to info@tradingboard.ai)

From your repo root (e.g. `TradingBoard.ai`):

```bash
cd basic_web/cloud_function
```

Deploy the function (replace the Gmail placeholders with your real values):

```bash
gcloud functions deploy sendFormEmail ^
  --gen2 ^
  --runtime python312 ^
  --region us-central1 ^
  --trigger-http ^
  --allow-unauthenticated ^
  --entry-point=send_form_email ^
  --set-env-vars "GMAIL_USER=your-sender@gmail.com,GMAIL_APP_PASSWORD=your-16-char-app-password,TO_EMAIL=info@tradingboard.ai"
```

- **GMAIL_USER**: the Gmail (or Google Workspace) address that sends the emails.  
- **GMAIL_APP_PASSWORD**: the 16-character App Password (no spaces in the env var).  
- **TO_EMAIL**: set to `info@tradingboard.ai` so all form submissions go there.  
- **No spaces** in the quoted env vars (e.g. use `,GMAIL_APP_PASSWORD=xxx` not `, GMAIL_APP_PASSWORD=xxx`).

On **PowerShell**, use backticks instead of `^` for line continuation:

```powershell
gcloud functions deploy sendFormEmail `
  --gen2 `
  --runtime python312 `
  --region us-central1 `
  --trigger-http `
  --allow-unauthenticated `
  --entry-point=send_form_email `
  --set-env-vars "GMAIL_USER=your-sender@gmail.com,GMAIL_APP_PASSWORD=your-16-char-app-password,TO_EMAIL=info@tradingboard.ai"
```

On **macOS/Linux** use backslash `\` at end of line instead of `^`.

**If deploy fails with "missing permission on the build service account"**, run this once (replace the project number with the one from the error message if different):

```bash
gcloud projects add-iam-policy-binding my-project-web-488416 --member=serviceAccount:444120615590-compute@developer.gserviceaccount.com --role=roles/cloudbuild.builds.builder
```

Then run the deploy command again.

After deploy, the CLI prints the function URL, e.g.:

```text
https://us-central1-my-project-web-488416.cloudfunctions.net/sendFormEmail
```

Copy that URL.

---

## Part E: Point the site at the function

Open **`basic_web/index.html`** and set `FORM_API_URL` to the URL from Part D:

```js
var FORM_API_URL = 'https://us-central1-my-project-web-488416.cloudfunctions.net/sendFormEmail';
```

Use your actual URL (region/project may differ).

---

## Part F: Deploy the static site to Google Cloud (Firebase Hosting)

Firebase Hosting is the simplest way to host the static site on Google Cloud.

1. **Install Firebase CLI** (once per machine):
   ```bash
   npm install -g firebase-tools
   ```

2. **Log in to Firebase** (if not already):
   ```bash
   firebase login
   ```

3. **From the `basic_web` folder**, init hosting (if needed) and deploy:
   ```bash
   cd basic_web
   firebase init hosting
   ```
   - “Use an existing project” → choose **my-project-web-488416**.  
   - “What do you want to use as your public directory?” → **.** (current directory).  
   - “Configure as a single-page app (rewrite all urls to /index.html)?” → **Yes** (so **tradingboard.ai/** and **www.tradingboard.ai/** serve the site at `/`).  
   - “Overwrite index.html?” → **No**.

   If **firebase.json** and **.firebaserc** are already in the repo (with the rewrite and project set), you can skip `firebase init hosting` and run **firebase deploy** from `basic_web`.

4. **Deploy**:
   ```bash
   firebase deploy
   ```

5. **Connect custom domains** (so **tradingboard.ai** and **www.tradingboard.ai** work at `/`):
   - Open [Firebase Console](https://console.firebase.google.com/) → project **my-project-web-488416** → **Hosting**.
   - Click **Add custom domain**.
   - Add **tradingboard.ai** → follow the steps (add the A records shown; Firebase will give you target hostnames, often `A` + `ALIAS` or similar).
   - Add **www.tradingboard.ai** (as a second custom domain or subdomain).
   - In **Namecheap** (or your DNS), for **tradingboard.ai** and **www**: point them to the **Firebase Hosting** targets shown in the console (replace the current load balancer A record `34.36.105.243` with the Firebase values). Firebase will provide the exact records.
   - Wait for SSL to provision (a few minutes to an hour). Then **https://tradingboard.ai/** and **https://www.tradingboard.ai/** will serve the site at `/`.

The CLI will show the live URL, e.g.:

```text
Hosting URL: https://my-project-web-488416.web.app
```

Open that URL; the forms will POST to your Cloud Function and emails will go to **info@tradingboard.ai**.

---

## Summary

| Step | What you do |
|------|------------------|
| A    | Install Google Cloud SDK (`gcloud`). |
| B    | `gcloud auth login` and `gcloud config set project my-project-web-488416`. |
| C    | Gmail: 2-Step Verification + App Password for the sending account. |
| D    | From `basic_web/cloud_function`: deploy with `gcloud functions deploy sendFormEmail ...` and set `TO_EMAIL=info@tradingboard.ai`. Copy the function URL. |
| E    | In `basic_web/index.html` set `FORM_API_URL` to that URL. |
| F    | From `basic_web`: `firebase init hosting` then `firebase deploy` to publish the site. |

After that, the site is on Google Cloud and form submissions are emailed to **info@tradingboard.ai**.
