# openhost-tasks-md

[Tasks.md](https://github.com/BaldissaraMatheus/Tasks.md) — a
markdown-file-based Kanban board — packaged as an OpenHost app
with seamless OpenHost SSO. The zone owner is auto-authenticated
by the auth-proxy; Tasks.md itself has no per-user auth, just a
filesystem of markdown files behind the SPA.

## What you get

- Tasks.md running on `https://tasks-md.<zone>/` with TLS
  terminated by the OpenHost outer Caddy.
- The zone owner is auto-authenticated; no login form ever
  appears.
- Persistent state under `/data/app_data/tasks-md/`:
  - `tasks/<lane>/<task>.md` — one Markdown file per task,
    one directory per lane.
  - `config/` — color themes, custom CSS, image uploads.
- Operators (and on-host LLM agents) can read and write the
  task files **directly** on the host filesystem; no API call
  needed.

## Why this app

For an "agent posts task progress that the human user can see
and edit" workflow, Tasks.md is the simplest possible primitive:
**tasks are markdown files in a directory tree**. An LLM agent
running on the same host (e.g. as another OpenHost app) can:

```bash
mkdir -p $OPENHOST_APP_DATA_DIR/../tasks-md/tasks/in-progress
echo "# Refactor login flow

Working on the SSO bounce in auth_proxy.py.

- [x] Read existing pattern
- [ ] Write the cookie-set redirect
- [ ] Test in browser
" > $OPENHOST_APP_DATA_DIR/../tasks-md/tasks/in-progress/refactor-login.md
```

…and the human will see the new card appear live in the SPA on
their next refresh. Drag it to "done" in the SPA → file moves
to `tasks/done/refactor-login.md` on disk. Edit the markdown
in the SPA → file rewrites on disk.

The directory-tree model also makes Tasks.md trivially git-
versionable, easy to back up, and easy to feed into other
tools (an LLM can grep through the tasks dir to find related
work, etc.).

## Architecture

```
browser
   │
   ▼
OpenHost outer Caddy (TLS)
   │
   ▼
OpenHost router (verifies zone_auth JWT;
                 stamps X-OpenHost-Is-Owner: true)
   │
   ▼
container :8090  ── auth_proxy.py ────────────────┐
                   • check                        │
                     X-OpenHost-Is-Owner: true    │
                   • 403 anything else            │
                   • forward owner traffic        │
                     verbatim                     │
                                                  │
                                                  ▼
                                       127.0.0.1:8080
                                       Tasks.md (Node.js + SolidJS SPA)
                                                  │
                                                  ▼
                                       /data/app_data/tasks-md/tasks/
                                       (markdown files on disk)
```

## Auth model

Tasks.md has no application-level authentication. The auth-
proxy is the only auth gate, and it does the simplest possible
thing: 403 unless the OpenHost router stamped
`X-OpenHost-Is-Owner: true` on the request.

Why this is safe even though the router's stamp is a single
plaintext header:

- The router strips client-supplied versions of
  `X-OpenHost-Is-Owner` and `X-OpenHost-User` before stamping
  its own (verified copy). A hostile client can't forge the
  header to bypass.
- `public_paths = []` in openhost.toml means the router 302's
  every anonymous request to `/login` before forwarding. The
  auth-proxy never sees anonymous traffic.
- The auth-proxy ALSO strips client-supplied trust headers as
  defence-in-depth, so even if the router has a bug or is
  bypassed, this layer rejects the request.

## Persistence

```
$OPENHOST_APP_DATA_DIR/
├── tasks/
│   ├── todo/
│   │   ├── refactor-login.md
│   │   └── ship-the-thing.md
│   ├── in-progress/
│   └── done/
└── config/
    ├── stylesheets/
    │   ├── custom.css
    │   └── color-themes/
    ├── images/
    └── sort.json
```

The persistent `tasks/` dir is the source of truth; the SPA
just renders it. An operator who wants to wholesale rearrange
their board can edit the directory tree on disk directly.

## API access (for agents)

Tasks.md exposes a small REST API at `/_api` for the SPA.
Agents on the same host should generally bypass the API and
edit files on disk directly — it's faster, has no auth
machinery to negotiate, and survives any future breaking
changes to the upstream API.

For agents NOT on the same host (or that prefer not to touch
the filesystem), the API is reachable through the auth-proxy
just like the SPA — the `Authorization: Bearer <openhost-
token>` header authenticates the request to the OpenHost
router, which stamps the owner header, which lets the auth-
proxy through.

## Limitations

- **No per-user auth = single-user.** Every visitor who passes
  the OpenHost zone JWT check has full read+write access.
  Fine for a personal zone; not a multi-tenant model. If
  multiple humans need separate boards, run multiple
  instances (one per user) or pivot to one of the heavier
  task apps (Vikunja, OpenProject, Plane, etc.).
- **No live updates.** The SPA polls (or you reload) to see
  changes. If an agent writes a task file, the human sees it
  on their next refresh, not instantly.
- **No mobile native app.** The SPA is responsive and works
  fine on phones; no separate native binary.
