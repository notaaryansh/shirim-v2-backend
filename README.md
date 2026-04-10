# shirim-v2-backend

Python/FastAPI backend for the shirim-v2 frontend. Serves curated GitHub repositories for the Home and Discover tabs, with AI-generated "smart summaries" built from each repo's README.

## Quick start

```bash
cd shirim-v2-backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# paste your OpenAI key into .env:
#   OPENAI_API_KEY=sk-...
# (optional) add a GITHUB_TOKEN to raise the GitHub API rate limit

uvicorn app.main:app --reload --port 8001
```

Docs: <http://127.0.0.1:8001/docs>

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | – | liveness |
| `POST` | `/api/v1/auth/send-otp` | – | Send a 6-digit OTP to email |
| `POST` | `/api/v1/auth/verify-otp` | – | Exchange email + OTP for access/refresh tokens |
| `POST` | `/api/v1/auth/refresh` | – | Exchange refresh_token for a new token pair |
| `POST` | `/api/v1/auth/sign-out` | Bearer | Invalidate the current session |
| `GET` | `/api/v1/auth/me` | Bearer | Return the current user |
| `GET` | `/api/home` | Bearer | Repositories for the Home tab (`Popular`, `Recently Run`) |
| `GET` | `/api/discover` | Bearer | Repositories for the Discover tab (`Productivity`, `AI`, `Trending`) |
| `GET` | `/api/repos/{owner}/{repo}` | Bearer | Full detail for one repo (metadata + summary + README images) |

## Auth

Email OTP login, backed by the **same Supabase project** as the sibling `shirim` app (credentials in `.env`). Access tokens are Supabase-signed ES256 JWTs validated via JWKS — no shared-secret needed. Access tokens expire in **1 hour** (`expires_in: 3600`); refresh with `/api/v1/auth/refresh`.

Set `DEV_BYPASS_AUTH=true` in `.env` to skip validation during local development — protected routes will return a fixed dev user without any token.

### Example flow
```bash
# 1. Request OTP
curl -X POST http://localhost:8001/api/v1/auth/send-otp \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'

# 2. Check your inbox, then verify
curl -X POST http://localhost:8001/api/v1/auth/verify-otp \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "otp": "123456"}'
# -> {"access_token": "...", "refresh_token": "...", "expires_in": 3600, "user": {...}}

# 3. Call a protected endpoint
curl http://localhost:8001/api/home \
  -H "Authorization: Bearer <access_token>"
```

### Response shape (`/api/home`, `/api/discover`)

```json
{
  "tab": "home",
  "categories": [
    {
      "name": "Popular",
      "repos": [
        {
          "id": 12345678,
          "name": "next.js",
          "repo": "vercel/next.js",
          "desc": "The React Framework for the Web",
          "language": "TypeScript",
          "stars": "118k",
          "summary": {
            "tagline": "...",
            "description": "...",
            "features": ["...", "..."],
            "categories": ["Web"],
            "install_difficulty": "easy",
            "requirements": ["Node.js 18+"]
          }
        }
      ]
    }
  ]
}
```

The top-level repo fields (`id`, `name`, `repo`, `desc`, `language`, `stars`) match the frontend's existing `MockProject` type, so swapping out the hardcoded mock data is a one-line change. The `summary` field is additive.

## Editing the repo lists

Edit `app/curated.py`. Category names must match the frontend's `VIEW_CATEGORIES` in `src/App.tsx` exactly.

## Summary cache

Smart summaries are generated on demand and cached to `cache/summaries/{owner}__{repo}.json`. The first request to `/api/home` will be slow (~20-30s) while OpenAI generates summaries for each repo. Subsequent requests are instant. Delete the cache directory to force regeneration.

Transient OpenAI failures return a minimal fallback summary but are **not** cached, so they self-heal on the next request.
