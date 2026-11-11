# Browser scenarios

How a bro that summons `@browser` words its request, keeps the browser for more than one answer, and gets a signed-in profile, a visible view, or a downloaded file.
Each section is one scenario;
the grammar behind the grants is `permissions`, the everyday setups are `permissions-cheatsheet`, and talk rights in general are the `ask` spell's.

## What the browser does

The browser navigates and extracts:
it opens pages, reads them and their screenshots, drives their controls, fills forms, downloads files, and reports what the pages show.
Choosing what to report by a criterion the request names, such as a price range or a keyword, is extraction.
Comparing, ranking, judging, recommending, summarising, or any other conclusion the pages do not state themselves is analysis, and it stays with the summoner:

- The browser's context goes to page sources, snapshots, and screenshots.
  An analysis brief competes with navigation for it and sends the browser through more pages than the question needs.
- The summoner holds what the analysis needs:
  the user's situation, earlier findings, and what the answer is for.
  A verdict the browser forms without that context is weaker, and the summoner then relays it as its own.
- A raw extract keeps each fact's own limits attached, such as "the page did not list the waybill" or "25 comments loaded".
  A summary drops them, so "this is not confirmation that screening has cleared" reaches the user as "they need nothing more".

The browser checks every request before it browses.
When any part of it asks for analysis, the browser refuses all of it, names the part out of scope, and suggests an extraction-only rewording:
as a question to the summoner when the quest grants `worker.question`, otherwise as the failure reason its run ends with.

## Word a request

Name the site or page, what to collect, and the criterion that selects it, and ask for the details the analysis will need as the page states them:

| Refused | Accepted |
|---|---|
| Which of jackets A and B is warmer? Recommend one. | Open the reviews of jacket A and of jacket B and list the reviews that mention warmth, with their ratings and dates. |
| Research what has happened to project X and summarise what backers report. | Open project X's updates and comments, list the three most recent updates with their dates, and quote the comments that mention shipping. |
| Is this listing a good deal? | Open the listing and report its price, condition, seller rating, and shipping cost, with a screenshot. |

## Ask once, steer, or drive step by step

A summoned browser closes its webview before it answers, and the answer ends its quest.
A follow-up after the answer is a new summon on a new webview, with none of the earlier page state.
Talk rights decide what can travel before the answer:

- A one-shot extraction, such as "open X and list the reviews with their ratings", needs only the default talk.
- Grant `owner.say` whenever someone may add to or correct the request while it runs, such as a user fixing a name the browser is typing into a form;
  the browser folds each message into its one answer.
  Without it the correction is refused, and applying it means cancelling the quest and summoning again after checking what the first run already submitted.
- Grant `worker.question` so the browser can ask instead of ending the run, for a consent choice, an ambiguous control, or the rewording of a refused request;
  the browser keeps its live page while the summoner answers.

For several answers from one open page, start the browser's own `browse` spell:
summon it with the prompt `[[browse]]`, naming the profile there if one is used, with `owner.question,worker.question` talk and an hours-sized timeout.
The browser opens the webview and waits.
Send each instruction as a question with `bro::quest_ask`;
its reply carries the outcome, where the browser is now, and the controls to name next.
It serves two ways of asking:

- a navigation goal with follow-ups — "find men's black t-shirts priced $20–50 and list them", then "open the 4th and send a screenshot";
- step-by-step direction — "open the site; what sections do you see?", then "open Men; what do you see?".

The browser asks about an instruction as a counter-question in its thread;
answer it with `bro::quest_ask` and `reply_to` set to the counter-question, so the outcome comes back as the reply.
Ask "done" to close the webview and receive the session log as the quest's answer.

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
Ctrl-C ends the run and leaves the stored profile unchanged,
so a run ended with Ctrl-C also lets the user look around the signed-in site by hand and save nothing.
`--port` fixes the noVNC port; `--indexed-db` also captures IndexedDB, which an existing seed that holds it keeps anyway.

`webview setup` refuses to run inside a managed session or without a terminal on stdin, and `webview open` is no substitute:
it needs a session's broker channel, and a webview mission never writes a profile.

## What persists

A webview mission starts from the profile and never writes it back.
A login made in a mission's browser, over VNC or by the browser's own actions, is gone when the mission ends.
A profile is renewed only by running `webview setup` again.

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
