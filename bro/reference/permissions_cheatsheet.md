# Permission cheat sheet

Everyday setups, without the model notation.
Run `ride` commands on the host; run `summon`, `webview open`, and `benchmark-run` inside the session unless stated otherwise.
Examples assume the named bros, harnesses, worker types, and credential kinds are installed, and the selected credential instances already exist.
Use your installed bro names in place of examples such as `assistant`, `helper`, or `reviewer`.
Each scenario is independent unless it explicitly continues the preceding one.

A `ride` flag changes that launch, not your dotfile.
JSON examples are complete `~/.bro.json` objects: merge the relevant keys into your existing file rather than overwriting unrelated settings.
A configuration edit reaches a fresh root or a resume, not a running tree.
For deeper details, see [permissions](permissions.md) or the abstract [model](permission_model.md).

## Let a session summon another bro, or stop it

On the host:

```sh
ride along --grant @browser assistant
ride along --revoke @browser assistant
```

The first session may ask browser to work; the second may not, even if assistant normally declares browser as a helper.
Inside the first session:

```sh
summon browser 'Find the current release notes.'
```

To make that choice persistent for assistant on one project:

```json
{
  "projects": {
    "https://github.com/me/project": {
      "bros": {"assistant": {"grant": ["@browser"]}}
    }
  }
}
```

A plain grant also lets assistant give that permission to its children.
To allow summoning without allowing it to hand the permission onward, use `"@browser for *"` instead.
To allow any bro target, use `"@*"`; it includes future installed names, not just today's roster.

## Give a session a credential, or choose an instance

On the host:

```sh
ride along --grant github assistant
ride along --cred github+reviewer assistant
ride along --revoke github assistant
```

The first adds the GitHub kind at its configured/default instance.
The second both adds the kind and selects `reviewer` for this session.
The third removes it.
These flags affect what the session reads; they do not authorize passing that instance to another mission.
`--cred github+` selects the empty instance explicitly.

To give assistant that instance on this project by default:

```json
{
  "projects": {
    "https://github.com/me/project": {
      "bros": {
        "assistant": {"grant": ["github"], "creds": ["github+reviewer"]}
      }
    }
  }
}
```

`grant` adds the kind; `creds` only chooses the instance.
If the bro already declares GitHub as a need, omit the `grant`.
Keep host instance choices out of the repository's `pyproject.toml`.

## Pass an instance to a child

On the host, allow assistant to start reviewer and select this instance for a bro child:

```sh
ride along --grant @reviewer --grant bro.cred=github+reviewer assistant
```

Inside that session:

```sh
summon --pass github+reviewer reviewer 'Review the patch.'
```

The child gets its own session store with GitHub selected as `reviewer`, and its install hook makes commands use that identity.
Assistant does not need to hold GitHub material in its own store to do this.
The passed value overrides the child's configured pick.
Passing does not let the child pass it onward; that needs a separate permission grant.
Without `--pass`, the child's own credential configuration supplies its picks.

## Let a bro open webviews, with a visible view or a profile

Browser already declares that it may start webviews.
For another bro with a shell or the webview tools, add `webview`.
On the host:

```sh
ride along --grant webview --grant webview.vnc --grant webview.cred=cookies+assistant assistant
```

Inside that session:

```sh
webview open --vnc --port 6080 --cookies assistant
```

`webview` permits opening the browser worker, `webview.vnc` permits a human-visible noVNC view, and the cookies permission selects that saved profile for the worker.
The command prints the mission id and a launcher-loopback VNC URL.
The port must be free; omit `--port` to choose one automatically.
Omit `--cookies` for an isolated browser without a saved profile; omit `--vnc` if no human view is needed.
Close the returned mission when finished with `webview close <mission-id>`.
Grant `webview.downloads` and add `--downloads` to let the page download files, each returned as an artifact ref in the first command reply after it finishes.
Profiles are made on the host with `webview setup assistant`.
Origin allow/block filters are browsing controls, not a permission security boundary.

## Give browser a profile without opening webviews yourself

Host configuration:

```json
{
  "projects": {
    "https://github.com/me/project": {
      "bros": {
        "assistant": {
          "grant": ["@browser", "webview.cred=cookies+assistant to @browser"]
        }
      }
    }
  }
}
```

Start assistant against that project:

```sh
ride along --repo https://github.com/me/project assistant
```

Inside assistant:

```sh
summon --grant webview.cred=cookies+assistant browser 'Open a webview with profile assistant and check my notifications.'
```

Browser receives usable permission for the profile and can call its webview open tool with `cookies` set to `assistant`.
Assistant gets no permission to open webviews itself and no cookies material in its session store.
This assumes its other applicable configuration does not already grant webview use.
The `to @browser` part means only browser children in separate parties may receive the profile right; it does not include joined children.
The plain summon grant forwards assistant's allowed recipients rather than widening them.
The allowed browser can act on assistant's instructions, so give-only means no direct use, not no delegated use.

## Relay the profile through a helper

For assistant to hand the same right through helper to browser, both possible recipients must be allowed:

```json
{
  "projects": {
    "https://github.com/me/project": {
      "bros": {
        "assistant": {
          "grant": ["@helper", "webview.cred=cookies+assistant to @helper @browser"]
        },
        "helper": {"grant": ["@browser"]}
      }
    }
  }
}
```

Inside assistant launched on that project:

```sh
summon --grant 'webview.cred=cookies+assistant to @browser' helper 'Ask browser to check notifications with profile assistant; grant it webview.cred=cookies+assistant.'
```

Helper receives only the right to give the profile to browser, not to use it itself.
Inside helper:

```sh
summon --grant 'webview.cred=cookies+assistant for *' browser 'Check notifications using profile assistant.'
```

Browser receives use with no onward give from this gift.
Assistant's recipient list must include browser too, because helper cannot receive broader onward recipients than assistant has.
This does not enforce a mandatory hop: assistant's profile permission also allows giving directly to browser if assistant separately has permission to summon it.

## Run a child on another provider

Install `bro-deepseek[client]` in the runtime and place the `deepseek` credential in the host store before starting the coordinator.
Check the prospective recipe on the host:

```sh
ride scope --bro bro-dev --harness bro --llm deepseek:pro:high
```

The descriptor names `provider: deepseek`, and the credentials show `deepseek+: PRESENT` or `MISSING`.
Inside a coordinator allowed to summon `bro-dev`:

```sh
summon --harness bro --llm deepseek:pro:high bro-dev 'Work on the repository-local task.'
```

Or call `bro::summon(target='bro-dev', harness='bro', llm='deepseek:pro:high', prompt='Work on the repository-local task.')`.
No new persona or provider grant is needed: provider, model and effort are open launch options.
The selected provider's LLM credential is held automatically at the configured pick;
`--pass deepseek+work` selects a different stored instance, and a missing key refuses the start.
A running root uses its frozen installation, so restart it after installing the plug-in.
DeepSeek processes data under its own terms, including storage in the PRC;
route repository-local work and keep untrusted web content out of that child's inputs.

## Let children join, or start unboxed

On the host:

```sh
ride along --grant @reviewer --grant bro.placement=join --grant bro.placement=unboxed assistant
```

Inside that session, choose one placement explicitly:

```sh
summon --join reviewer 'Inspect the current uncommitted patch.'
summon --unboxed reviewer 'Investigate in a separate host-side clone.'
```

A join sees the same tree and uncommitted files and inherits the party's isolation.
An unboxed start gets a separate workspace but runs as the host user; credential scoping there is a convenience, not a security boundary.
Neither is implied by the other.
An ordinary summon still chooses boxed, even if only unboxed is allowed.
For join-only children, start with `--revoke bro.placement=boxed --grant bro.placement=join` and use `summon --join`.
A join shares files and processes; do not rely on give-only rights to isolate it from its starter.

## Restrict a child

Inside a session allowed to summon reviewer:

```sh
summon --revoke @browser --revoke bro.placement=unboxed --revoke github reviewer 'Review without GitHub access or browser delegation.'
```

The child cannot summon browser or start unboxed children, and its GitHub kind is dropped even if normally configured.
Its already-independent base is otherwise unchanged; a child does not inherit every permission of its parent.
To let reviewer keep its own permission to summon browser but stop it giving that permission onward:

```sh
summon --limit '@browser for *' reviewer 'Review; do not give browser delegation to your helpers.'
```

This limit keeps use of the browser target and removes give for that target.
It does not grant browser if reviewer has no such permission already.
Limits constrain only the permissions they name; unrelated permissions remain unchanged.

## Run a benchmark

On the host:

```sh
ride along --repo /home/me/project --grant benchmark assistant
```

Inside that attached workspace, with this checkout's benchmark presets (or your own corresponding preset names):

```sh
benchmark-run --agents terminal-bro-gpt-5.6-terra-high --settings smoke --tasks smoke --timeout 3600
```

The session asks the host to run the registered benchmark worker; it does not need a Docker socket in its container.
`benchmark` permits the worker start, and the open timeout option permits this one-hour bound unless capped.
The result is retained in the mission journal and delivered through the session watch.
Configuration preparation and result commands are in the [benchmark guide](../../benchmark/README.md).

## Change a running session

On the host, find the root with `ride list` and its tree with `ride missions <root>`.
Use the mission id from `ride missions` in place of `my-session`:

```sh
ride override my-session --grant @browser
ride override my-session --cred github+reviewer
ride override my-session --revoke github
ride override my-session --revoke @browser
```

These respectively enable browser delegation, switch/add GitHub, remove GitHub, and stop future browser summons.
The session watch reports the change; children already running keep what they have.
Only bro targets and session credentials can change this way; other permissions and launch options need a new start or resume.
The root host must still be alive.

Later store reads and newly started commands see a published credential change; already-read values and start-time clients do not change.
The reply names effects that need the next start, such as the LLM client's key or Claude authentication.
`applied` means publication completed; `uncertain` names a pending operation, not a failure you should overwrite with another override.
Let the host settle it from the applier receipt; pending operations block another override of that mission.
Spawned workspaces with credential-operation records remain after clean completion for settlement or resume, while private credential stores are removed.
Use `ride clean` to remove those kept records when finished.

## Resume with different settings

On the host:

```sh
ride resume my-session --cred github+reviewer --grant @browser
```

Resume starts a new root using current configuration and installation, reapplying defaults such as unpinned credential picks and the default recipe.
It keeps explicit recorded values and gifts/caps unless the resume flags replace them.
Former children do not survive a resume, and a resumed child becomes its own root.
Use override rather than resume when a running session only needs a new target or credential and should keep its current tree alive.

## Guard your own operator actions

To prevent yourself from starting unboxed bro sessions:

```json
{
  "operator": {"revoke": ["bro.placement=unboxed"]}
}
```

This checks your own root launches and manual commits as well as overrides and resumes.
It does not remove a child's independent base authority.
To also prevent sessions from starting unboxed children, put the same revoke in applicable session policy:

```json
{
  "operator": {"revoke": ["bro.placement=unboxed"]},
  "defaults": {"revoke": ["bro.placement=unboxed"]}
}
```

Later session configuration grants may restore an earlier session-policy revoke.
The operator restriction is checked separately and cannot be bypassed by adding a root launch grant.
It is a guard against mistakes under your host account, not protection from another process running as that account.

## See what is allowed

On the host:

```sh
ride scope --repo /home/me/project --bro assistant
ride missions my-session
```

`scope` shows the prospective descriptor, base permissions, picks, and whether instances are present, missing, or optional-and-skipped.
An instance named only by permission may be absent without making scope inspection fail.
`missions` lists a running tree's names for operations such as override.
Inside a session, call `bro::banner` or run:

```sh
ride banner --llm
```

`may_summon` names usable bro targets, `permits` other usable permissions, and `may_give` rights you can hand out but not use.
Conditional entries still require the requested start to match their conditions.
The banner reads live host authority, not just the initial configuration.
A plain `ride exec` shell has no mission channel, so its banner cannot show authority rows; ask from the agent session instead.

## Launch a child yourself

Inside a session allowed to summon reviewer:

```sh
summon --manual --talk owner.say,owner.question,worker.question reviewer 'Work with me on the review.'
```

Relay the returned runtime-specific `ride along --summoned <token> reviewer` command to the person who will launch it.
On the host, run that exact command; add `--unboxed`, `--harness`, `--llm`, `--hold`, or `--cred` there if needed.
For example, insert `--cred github+reviewer` before the final `reviewer` positional to choose that identity.
The operator owns those final choices, so the summoner needs no placement right for a manual child.
The final launch is rechecked against both parties' permissions, using the root's original configuration reading.
The request's target, talk, gift, and cap cannot be rewritten on that command line.
Use `ride override` for supported target/credential changes after the child starts.
When the work is done the child delivers its answer to the summoner, just like a spawned child.
