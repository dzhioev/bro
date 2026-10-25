# Browser scenarios

How a bro that summons `@browser` gets a signed-in profile, a visible view, or a downloaded file.
Each section is one scenario;
the grammar behind the grants is `permissions` and the everyday setups are `permissions-cheatsheet`.

## Give the browser a profile

A profile is a saved set of cookies and page storage, stored on the launcher host as `cookies+<profile>`.
Host configuration lets the summoning bro give it, and `may_give` in the banner lists what it can give:

```json
"grant": ["@browser", "webview.cred=cookies+<profile> to @browser"]
```

The summon forwards the right:

```sh
summon --grant webview.cred=cookies+<profile> browser '<request>'
```

Through `bro::summon`, the same is `grant: ["webview.cred=cookies+<profile>"]`.

The summon's prompt must name the profile.
The browser opens a profile only when its request names one;
a prompt that says only "the cookies profile you were granted" leaves it guessing, and a guessed name outside the grant is refused.
Write "Open a webview with the cookies profile `<profile>` and …".

## Sign a site into a profile

Only the user can do this, on the launcher host, outside any managed session, from a terminal:

```sh
webview setup <profile> --url <sign-in page>
```

It starts a capture browser seeded from the stored profile (`--fresh` starts empty) and prints a noVNC address.
The user signs in through that view, then presses Enter to save the profile under `cookies+<profile>`.
Ctrl-C ends the run and leaves the stored profile unchanged.
`--port` fixes the noVNC port; `--indexed-db` also captures IndexedDB, which an existing seed that holds it keeps anyway.

`webview setup` refuses to run inside a managed session or without a terminal on stdin, and `webview open` is no substitute:
it needs a session's broker channel, and a webview mission never writes a profile.

## What persists

A webview mission starts from the profile and never writes it back.
A login made in a mission's browser, over VNC or by the browser's own actions, is gone when the mission ends.
A profile is renewed only by running `webview setup` again.

## Browse with a profile by hand

- The `browse` spell: summon the browser with `owner.question,worker.question` talk and an hours-sized timeout, and the owner directs it one instruction at a time while it keeps one webview open.
- `webview setup <profile>` ended with Ctrl-C: the user looks around the signed-in site in the capture browser and saves nothing.

## Reach the view on a remote launcher host

The noVNC view, from `webview setup` or a webview opened with VNC, binds to the launcher host's loopback.
When the launcher is a remote host (`$RIDE_HOST` names it), forward the view's port from the user's machine:

```sh
ssh -L <port>:localhost:<port> <launcher host>
```

then open the printed address in the user's own browser, since `127.0.0.1` there reaches the forwarded port.
Pass `--port` so the port is known before the run starts.

## Download a file

Page downloads are the guarded `webview.downloads` option, for example a file from a signed-in iCloud folder handed back as an artifact ref.
Host configuration gives the summoning bro the right to give it, next to the profile right a signed-in page needs:

```json
"grant": ["@browser", "webview.downloads to @browser", "webview.cred=cookies+<profile> to @browser"]
```

The summon forwards it:

```sh
summon --grant webview.downloads --grant webview.cred=cookies+<profile> browser '<request>'
```

Through `bro::summon`, `grant: ["webview.downloads", "webview.cred=cookies+<profile>"]`.
The prompt names the profile and says which file to download;
the browser opens the webview with `downloads` only when the request needs a file.

A download finishes after the action that starts it, so that action can reply before it does or time out while it runs.
The artifact ref arrives in the first later reply that reports "Downloaded file".
The browser keeps issuing commands, such as a snapshot, until one does, and reports the ref.
An owner-led session keeps asking for the same until the file appears.
