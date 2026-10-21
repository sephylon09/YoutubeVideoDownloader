# Media Downloader

A web app for saving YouTube videos as **MP4** (every resolution the video offers, up to 4K/8K) or
**MP3** audio. It runs on your own PC at <http://127.0.0.1:8000>, with no accounts, no ads and no cloud
services. It can also be deployed as a private, password-protected website on
[Render](#deploying-to-render) (or anywhere Docker runs).

> Only download videos you own or have permission to download. The app does not bypass DRM, paywalls,
> logins or age/member restrictions.

---

## Quick start (Windows)

1. Install **Python 3.10 or newer** (see [Installing the requirements](#installing-the-requirements)).
2. Double-click **`start.bat`**, or run it from the VS Code terminal:

   ```powershell
   .\start.bat
   ```

`start.bat` checks Python and creates a virtual environment (`.venv`). It installs the Python packages
(FastAPI, `yt-dlp[default]` with yt-dlp-ejs, and Deno) and checks FFmpeg. If FFmpeg is missing it offers
to install it with `winget`. Then it starts the server and opens your browser. Keep the window open while
you use the app; press **Ctrl+C** to stop it.

In VS Code you can also use **Terminal → Run Build Task… (Ctrl+Shift+B) → "Start Media Downloader"**, or
the **Run and Debug** panel → "Media Downloader (debug server)".

---

## Using the app

### Download an MP4

1. Paste a YouTube link (Ctrl+V or the **Paste** button) and press **Analyze Video** (or Enter).
2. The video's thumbnail, title, channel, duration and available resolutions appear.
3. Keep the **MP4 · Video** tab selected and choose a quality card. **Best quality** (recommended) picks
   the highest resolution and frame rate available.
4. Click **Download MP4**. Progress, speed and time remaining appear under **Downloads**.
5. When it finishes, click **Download File** to save it through your browser.

### Download an MP3

1. Analyze the video as above.
2. Switch to the **MP3 · Audio** tab.
3. Choose **Best Source Audio** (recommended) or 128/192/256/320 kbps.
4. Click **Download MP3**, then **Download File** when it's ready.

You can start several downloads (for example MP4 and MP3 of the same video), analyze another video while
downloads are running, cancel a running download, and retry failed ones. Downloads continue if you reload
the page.

### What the quality cards mean

| Label | Meaning |
| --- | --- |
| **1080p60**, **2160p (4K)** … | Resolutions taken from the formats YouTube actually returned for *this* video. Nothing is hard-coded. |
| Codec (H.264 / VP9 / AV1) | The video codec that will be in the MP4. H.264 is preferred when it exists at that resolution. |
| ≈ size | Estimated file size (YouTube doesn't always report an exact size). |
| **No conversion** | Streams are copied into the MP4 unchanged: fast and lossless. |
| **Audio → AAC** | Video copied as-is; only the audio is converted to AAC (quick). |
| **Re-encode · slower** | The video is re-encoded to H.264 (only when you turn on the H.264 option). |

---

## How it works

* **Separate video and audio streams.** Above 360p, YouTube serves video-only and audio-only streams.
  The app downloads the chosen video stream and the best audio stream in the video's original language,
  then merges them with FFmpeg. Every MP4 is checked with ffprobe and must contain an audio track before
  it is marked as completed.
* **MP4 compatibility.** H.264 (up to 1080p) and AAC audio are copied straight into the MP4. Higher
  resolutions only exist as VP9 or AV1. Both are valid inside MP4 and are stream-copied without quality
  loss. They play in modern browsers, VLC and Windows Media Player (Windows may need the free *VP9 Video
  Extensions* or *AV1 Video Extension* from the Microsoft Store). For older TVs, phones or video editors,
  turn on **Convert video to H.264**. This re-encodes with FFmpeg (libx264), takes much longer and is
  offered up to 2160p for SDR video.
* **Resolutions are never silently reduced.** If the selected quality is no longer offered, the download
  fails with a clear message instead of quietly picking something lower.
* **MP3.** YouTube doesn't provide MP3 files, so the best source stream (usually Opus at about 130 kbps)
  is converted with LAME. The bitrate options are *encoding settings*. A higher bitrate reduces conversion
  loss but can't make the audio better than the source, and options above the source bitrate are marked.
  **Best Source Audio** uses LAME VBR V0, the standard high-quality setting (about 220–260 kbps). MP3s
  include the title, channel, year and the video thumbnail as cover art.
* **Live streams, premieres and playlists** aren't supported. Only single videos are processed; playlist
  parameters in a link are ignored.

### Project structure

```
app.py                     FastAPI app (entry point app:app): pages, API, sign-in, security middleware, server
media_downloader/
  config.py                Settings (overridable with MD_* environment variables; local vs. public mode)
  security.py              Password check, signed session cookies, in-memory rate limiting
  validation.py            Strict YouTube URL validation -> canonical URL
  downloader.py            yt-dlp Python API: metadata extraction, raw stream downloads, server policy checks
  formats.py               Builds quality options from real formats; resolves a choice to format IDs
  ffmpeg.py                FFmpeg/ffprobe commands (argument lists, no shell), progress, cancellation
  pipeline.py              One job: info -> plan -> limits -> download -> merge/convert -> verify -> publish
  jobs.py                  Job state machine + background worker pool (queue limit, job timeout)
  storage.py               Safe filenames, path checks, cleanup of old files
  environment.py           Finds FFmpeg/ffprobe/Deno (also in WinGet and registry PATH locations)
  errors.py                Friendly error messages for common failures
  doctor.py                Dependency check used by start.bat and the Docker build
templates/                 Page markup (index.html, login.html)
static/css, static/js      Styles and front-end logic (no build step)
tests/                     Automated tests (pytest)
downloads/                 Finished files (temporary, cleaned automatically)
logs/app.log               Detailed diagnostic log
start.bat / update.bat     Windows launch / update scripts
Dockerfile, .dockerignore  Production container image
render.yaml                Render Blueprint (one-click deployment settings)
.github/workflows/ci.yml   GitHub Actions: tests on Linux + Docker build and smoke test
```

### API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/api/analyze` | `{"url": "..."}` → video metadata, MP4 options, MP3 options, server limits |
| `POST` | `/api/download` | `{"url", "format": "mp4"\|"mp3", "quality", "convert_h264"}` → `{"job_id"}` |
| `GET` | `/api/progress/{job_id}` | Status, stage, percent, bytes, speed, ETA, steps, errors |
| `GET` | `/api/file/{job_id}` | The finished file (streamed, correct filename) |
| `POST` | `/api/cancel/{job_id}` | Cancel a queued or running job |
| `GET` | `/api/health` | Tool versions and problems (FFmpeg, Deno, yt-dlp) |
| `GET` | `/healthz` | Health check for the hosting platform: `{"status": "ok"}` or HTTP 503. No sign-in needed. |
| `GET`/`POST` | `/login`, `POST /logout` | Sign-in page and sign-out (only when a password is set) |

When a password is set, every page and API call except `/login`, `/healthz` and static files requires a
signed-in session. API calls without one get HTTP 401.

`quality` is `best` or an option ID returned by `/api/analyze` (for example `1080p60`, `2160p60-hdr`) for
MP4, and `best`, `320`, `256`, `192` or `128` for MP3. Requests are validated against the video's actual
formats.

---

## Installing the requirements

The app needs **Python**, **FFmpeg (with ffprobe)** and a **JavaScript runtime (Deno)**. `start.bat`
installs Deno automatically into the project's `.venv` through the official `deno` PyPI package and can
install FFmpeg via winget. Manual steps are below if you prefer them or if something fails.

### 1. Python 3.10+

1. Download the Windows installer from the official site: <https://www.python.org/downloads/windows/>
2. Run it and tick **"Add python.exe to PATH"**. Keep "Install launcher" (`py`) enabled.
3. Open a **new** terminal and verify:

   ```powershell
   py --version        # or: python --version
   ```

### 2. FFmpeg and ffprobe

**Option A: winget (recommended).** In a terminal:

```powershell
winget install --id Gyan.FFmpeg -e
```

**Option B: manual download.**

1. Go to the official download page <https://ffmpeg.org/download.html> → Windows → *Windows builds from
   gyan.dev*, or directly to <https://www.gyan.dev/ffmpeg/builds/>.
2. Download **ffmpeg-release-essentials.zip** and extract it.
3. Either copy `ffmpeg.exe` and `ffprobe.exe` from its `bin` folder into this project's
   `tools\ffmpeg\bin\` folder (no PATH changes needed), **or** add the extracted `bin` folder to your PATH:
   Start → "Edit the system environment variables" → Environment Variables → *Path* → Edit → New → paste
   the folder path → OK.

Verify in a **new** terminal (PATH changes don't reach terminals that were already open; restart VS Code
too):

```powershell
ffmpeg -version
ffprobe -version
where.exe ffmpeg
```

The app also finds FFmpeg installed by winget even if your current terminal hasn't picked up the new PATH
yet. You can point to a custom location with the `MD_FFMPEG_LOCATION` environment variable.

### 3. JavaScript runtime (Deno)

YouTube requires a JavaScript challenge to be solved before most formats can be downloaded. yt-dlp does
this with its **yt-dlp-ejs** scripts, which need a JavaScript runtime. **Deno** is recommended and enabled
by default (see the official yt-dlp documentation: <https://github.com/yt-dlp/yt-dlp/wiki/EJS>).

* Automatic: `start.bat` installs the official Deno binary into `.venv` (`pip install deno`).
* Manual alternative: `winget install --id DenoLand.Deno -e`, or see
  <https://docs.deno.com/runtime/getting_started/installation/>. Verify with `deno --version` (2.3.0+).
* Node.js 22+ is used automatically as a fallback if Deno isn't available.

### 4. Python packages (manual setup)

`start.bat` does this for you. To do it by hand:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py --open
```

`requirements.txt` installs `yt-dlp[default]`, which includes the matching **yt-dlp-ejs** version.

---

## Keeping yt-dlp up to date

YouTube changes often. When analyzing or downloading starts to fail, update yt-dlp first:

```powershell
.\update.bat
```

The page shows a notice when a newer yt-dlp release is available on PyPI.

---

## Configuration

Optional environment variables (set them before running `start.bat`, or in the Render dashboard).
"Docker" shows the value the [Dockerfile](Dockerfile) sets for hosting; `0` means unlimited.

| Variable | Default | Docker | Meaning |
| --- | --- | --- | --- |
| `MD_HOST` | `127.0.0.1` | `0.0.0.0` | Interface to listen on. Anything other than 127.0.0.1/localhost switches on **public mode** (password required). |
| `MD_PORT` | `8000` | – | Port. Locally, if it is busy the next free port is used. |
| `PORT` | – | `8000` | Port chosen by the hosting platform (Render sets it). Only used in public mode, and `MD_PORT` wins. |
| `MD_PASSWORD` | – | **required** | Password for the sign-in page. At least 10 characters; use a long random one. Required in public mode. |
| `MD_SECRET_KEY` | random per start | – | Key that signs session cookies. Set it (Render generates one) so restarts don't sign everyone out. |
| `MD_SESSION_HOURS` | `12` | – | How long a sign-in lasts. |
| `MD_ALLOWED_HOSTS` | – | – | Extra host names the app answers to, comma-separated (e.g. your custom domain). On Render, `RENDER_EXTERNAL_HOSTNAME` (`xyz.onrender.com`) is added automatically. |
| `MD_TRUST_PROXY` | on when on Render | – | Take the client IP and `https` from the proxy's `X-Forwarded-*` headers. |
| `MD_RATE_LIMITS` | on in public mode | – | Per-user hourly limits below, plus a 600 requests/minute flood guard per IP. Sign-in attempts are always limited (10 failures per IP per 15 min, 30 in total per 5 min). |
| `MD_ANALYSES_PER_HOUR` | `60` | – | Video analyses per signed-in session per hour. |
| `MD_DOWNLOADS_PER_HOUR` | `20` | – | Downloads started per signed-in session per hour. |
| `MD_RETENTION_HOURS` | `24` | `1` | Finished files older than this are deleted. |
| `MD_MAX_CONCURRENT_DOWNLOADS` | `2` | `1` | Downloads processed at the same time (others wait in a queue). |
| `MD_MAX_CONCURRENT_ANALYSES` | `4` | `2` | Video analyses running at the same time. |
| `MD_MAX_PENDING_JOBS` | `20` | `6` | Queued + running downloads accepted at once; more are refused ("Too many downloads"). |
| `MD_MAX_FILE_MB` | `0` | `1024` | Largest file the server will create. Larger qualities are greyed out, and a download whose size wasn't known in advance is stopped when it crosses the limit. |
| `MD_MAX_STORAGE_MB` | `0` | `3072` | Total space the downloads folder may use (finished files + running jobs). |
| `MD_MAX_DURATION_MINUTES` | `0` | `180` | Longest video the server will process. |
| `MD_JOB_TIMEOUT_MINUTES` | `0` | `60` | A download (including merging/conversion) running longer than this is stopped and marked failed. |
| `MD_ALLOW_H264` | `1` | `0` | Offer the CPU-heavy "Convert video to H.264" option. |
| `MD_ALLOWED_CHANNELS` | – | – | If set, only videos from these channels can be downloaded, e.g. `@MyChannel,UCxxxxxxxxxxxxxxxxxxxxxx`. |
| `MD_PROXY` | – | – | Proxy for yt-dlp's YouTube traffic, e.g. `http://user:pass@host:port` (see [platform restrictions](#youtube-and-cloud-servers)). |
| `MD_DOWNLOAD_DIR` | `downloads` | `/data/downloads` | Where finished files are stored. |
| `MD_LOG_DIR` / `MD_CACHE_DIR` | `logs` / `.cache` | `/data/logs` / `/data/cache` | Log file and yt-dlp cache. |
| `MD_FFMPEG_LOCATION` | auto | – | Folder containing `ffmpeg` and `ffprobe`. |
| `MD_DENO_PATH` | auto | – | Path to the Deno executable. |
| `MD_NO_BROWSER` | – | – | Set to `1` to stop `start.bat` opening the browser. |

## Files, cleanup and security

* Finished files are saved in `downloads\` as `Video Title.mp4` / `.mp3`. Titles are sanitised for
  Windows (Unicode and emoji are kept), shortened to fit Linux's 255-byte file name limit when hosted, and
  duplicates become `Video Title (2).mp4`.
* In-progress data lives in `downloads\.work\<job-id>\` and is deleted when a job completes, fails, times
  out or is cancelled, and again on every start.
* Finished files are deleted automatically after `MD_RETENTION_HOURS` (checked every 10 minutes, more
  often for short retention times, and at startup). Copy anything you want to keep, or just use the
  browser's **Download File** button.
* Locally the server listens on 127.0.0.1 only. Without `MD_PASSWORD` it refuses every connection that
  doesn't come from the same computer, however it was started. In public mode it refuses to start
  without a password of at least 10 characters.
* It rejects requests from other websites (Origin / Host checks), never passes user input to a shell, and
  only serves files it created inside the downloads folder, through `/api/file/<random job id>` with the
  real file name in `Content-Disposition`.
* Only single YouTube videos are accepted (youtube.com, youtu.be, Shorts, embeds). Playlists, channels,
  live streams, DRM, private, members-only and age-restricted videos are refused, and no cookies or
  YouTube sign-in are used. With `MD_ALLOWED_CHANNELS` you can limit the server to channels you own or
  have permission for.

---

## Deploying to Render

The repository includes everything Render needs: a [Dockerfile](Dockerfile) (Python 3.13 on Debian,
FFmpeg + ffprobe from Debian, `yt-dlp[default]` with yt-dlp-ejs, and Deno from the official `deno` PyPI
package), a [render.yaml](render.yaml) Blueprint and a `/healthz` health check. Nothing Windows-specific
is used: `start.bat`, `update.bat` and `run-tests.bat` are only for local use.

### 1. Publish the code on GitHub

`.gitignore` keeps downloaded files, `.venv`, logs, caches, cookies, `.env` files and other secrets out of
the repository. Check with `git status` before the first commit that nothing private is listed.

```powershell
cd C:\Projects\YoutubeVideoDownloader
git add .
git status                      # review: no downloads\, logs\, .venv\ or secrets
git commit -m "Media Downloader with Render deployment"
```

Create an **empty private repository** on <https://github.com/new> (no README, .gitignore or license, so
the histories don't clash), then:

```powershell
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

GitHub then runs the workflow in `.github/workflows/ci.yml` (tab **Actions**): the tests on Linux, a Docker
image build, and a smoke test of the running container. Wait for it to turn green.

### 2. Create the service on Render

**Option A: Blueprint (recommended).**

1. Sign in at <https://dashboard.render.com> and connect your GitHub account when asked.
2. **New → Blueprint**, choose the repository, and keep the Blueprint path `render.yaml`.
3. Render asks for **`MD_PASSWORD`**: enter a long random password (at least 10 characters).
   `MD_SECRET_KEY` is generated automatically.
4. Click **Apply** / **Deploy Blueprint**. The first build takes a few minutes.

The Blueprint uses the **free** instance type. Free instances have very little CPU and go to sleep after 15
minutes without visitors; waking up takes about a minute and loses running jobs and stored files. For real
use, switch the service to a paid instance type under **Settings → Instance Type**. Keep the instance count
at **1**: jobs and sessions live in the server's memory.

**Option B: manual Web Service.** **New → Web Service** → pick the repository → **Language: Docker** →
choose a region and instance type → **Advanced**: Health Check Path `/healthz` → add the environment
variables `MD_PASSWORD` (your password) and `MD_SECRET_KEY` (a long random string) → **Create Web
Service**.

### 3. Use it

Open `https://<service-name>.onrender.com`, sign in with your password, and use the app as usual. The page
only talks to its own origin, so it works on the HTTPS address (and on a custom domain: add the domain
under **Settings → Custom Domains** and also put it in `MD_ALLOWED_HOSTS`).

Every push to `main` redeploys automatically. Redeploy now and then (**Manual Deploy → Clear build cache &
deploy**) to pick up new yt-dlp releases: this replaces `update.bat` on the server.

### Storage on Render is temporary

Render's filesystem is ephemeral: everything in `/data` disappears on every deploy, restart and (on the free
type) when the instance sleeps. Finished files are meant to be downloaded right away and are deleted after
`MD_RETENTION_HOURS` (1 hour) anyway. Download history in the browser points to files that may no longer
exist; the page then says the file is gone. Nothing is stored permanently, and adding a Render disk doesn't
change that because of the retention cleanup.

### YouTube and cloud servers

YouTube treats traffic from data centres (Render, AWS, Google Cloud and so on) with much more suspicion
than home connections. Expect some or all requests from a hosted server to fail with **"YouTube asked
this server to confirm it isn't a bot"** (or HTTP 403/429), even when the same video works on your PC.
This depends on the server's IP address and can change from day to day; it is not something the app can
fix. This app deliberately doesn't use YouTube cookies or accounts (that risks the account being blocked).
Options if it happens:

* Run the app locally with `start.bat` (works reliably from a home connection).
* Route yt-dlp through a proxy you're allowed to use with `MD_PROXY` (store it as a secret environment
  variable on Render; it can contain credentials).
* Try another Render region (requires a new service).

### Run the container locally (optional)

With Docker Desktop installed:

```powershell
docker build -t media-downloader .
docker run --rm -p 8000:8000 -e MD_PASSWORD="choose-a-long-password" media-downloader
```

Then open <http://localhost:8000>.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| "FFmpeg is not installed" | Install FFmpeg (see above), then restart `start.bat`. |
| "JavaScript runtime required" / few qualities listed | Run `start.bat` again (reinstalls Deno) and `update.bat`. |
| "YouTube blocked the request" / HTTP 403 / 429 | Wait a while, then run `update.bat`. Signing in/cookies are intentionally not supported. |
| Age-restricted, private or members-only video | Not supported. These require signing in. |
| VP9/AV1 MP4 won't play in an old player | Turn on **Convert video to H.264**, or use VLC. |
| Port 8000 already in use | The app picks the next free port automatically; check the console for the URL. |
| Browser download never finishes (stuck as `.crdownload`) | Windows limits full paths to 260 characters. If your browser's download folder is very deeply nested, choose a shorter one in the browser settings. |
| Render deploy fails at "Refusing to listen … without a password" | Set `MD_PASSWORD` (10+ characters) under **Environment** and redeploy. |
| Hosted: "Invalid host header" | Add your custom domain to `MD_ALLOWED_HOSTS`. |
| Hosted: "confirm it isn't a bot" on every video | YouTube is blocking the server's IP address; see [YouTube and cloud servers](#youtube-and-cloud-servers). |
| Hosted: download disappeared / "File no longer available" | The retention time passed or the server restarted. Download again. |
| Anything else | See `logs\app.log` (locally) or the **Logs** tab on Render. |

## Running the tests

```powershell
.\run-tests.bat                         # offline tests (URL validation, formats, jobs, paths, API, FFmpeg, hosting)
$env:MD_NETWORK_TESTS = "1"; .\run-tests.bat   # also downloads a tiny real video from YouTube
```

On Linux/macOS: `pip install -r requirements-dev.txt && python -m pytest`.
