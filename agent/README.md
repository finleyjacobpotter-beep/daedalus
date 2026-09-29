# daedalus-agent

The AI agent that ships in the Daedalus image, built on
[tau](https://github.com/huggingface/tau) (`tau-ai` on PyPI): tau's provider
layer (`tau_ai`) talks to the model and its agent loop (`tau_agent`) runs the
tool calls. Daedalus adds an explicit tool registry and two team modes,
**hyper planning** and **ultrawork**.

## No tools unless you name them

A fresh agent has **no tools at all**. Each one is enabled by name, per run:

```sh
daedalus-agent tools                                   # list what exists
daedalus-agent run "explain tau's agent loop"          # no tools: talks only
daedalus-agent run -t read_file -t grep "where is the retry logic?"
daedalus-agent run -t read_file,edit_file,run_bash "fix the failing test"
DAEDALUS_AGENT_TOOLS=read_file,grep daedalus-agent run "..."
```

There is no `all` shortcut. Unknown names are an error.

| Tool | Kind | What it does |
|---|---|---|
| `read_file` | read-only | read a text file, with line numbers and paging |
| `list_dir` | read-only | list a directory |
| `glob` | read-only | find files by pattern |
| `grep` | read-only | regex search over file contents |
| `web_fetch` | read-only | HTTP(S) GET; HTML is turned into text. Goes through `HTTPS_PROXY`, so in `make agent` only allowlisted domains work |
| `write_file` | files | create or overwrite a file |
| `edit_file` | files | replace an exact snippet in a file |
| `run_bash` | exec | `bash -c` in the workspace, with a timeout |
| `run_python` | exec | run a Python script in the workspace, with a timeout |
| `delegate` | team | hand sub-tasks to sub-agents that run in parallel |

File tools are confined to the workspace (`-w`, default `.`); symlinks that
point outside it are refused too. `--allow-outside-workspace` lifts that.
Writes from parallel agents are serialised per file.

Sub-agents, planners, workers and verifiers never get a tool you did not
enable. Planning roles only get the read-only subset; verifiers get the
read-only and exec tools but cannot write.

## Modes

### `run`: one agent

```sh
daedalus-agent run -t read_file,grep "question"
echo "question" | daedalus-agent run -t read_file
daedalus-agent run -t read_file,edit_file         # interactive session
```

### `hyperplan`: a team writes the plan

```sh
daedalus-agent hyperplan -t read_file,list_dir,glob,grep --save plan.json "add rate limiting to the API"
```

1. **Research.** Up to four researchers read the workspace in parallel, each
   through a different lens (layout, constraints, verification, prior art).
2. **Plan.** Up to six planners write competing plans in parallel, each with a
   different strategy (minimal, parallel, robust, test-first, incremental,
   architect).
3. **Critique.** One critic per plan scores it and lists problems, in parallel.
4. **Synthesis.** A lead planner merges the best of all candidates into one plan.
5. **Review.** Reviewers score the merged plan; it is revised until they all
   accept it or `--review-rounds` runs out.

The plan is a DAG of tasks with dependencies, owned files, tools and
acceptance criteria. Tasks that could run at the same time but claim the same
file are serialised automatically. Tune with `--researchers`, `--planners`,
`--no-critique`, `--review-rounds`, `--reviewers`.

### `ultrawork`: plan with a team, build with a team

```sh
daedalus-agent ultrawork -t read_file,list_dir,glob,grep,edit_file,write_file,run_bash \
    "add rate limiting to the API, with tests"
daedalus-agent ultrawork -t ... --plan plan.json     # execute a saved plan
```

1. Hyper planning produces the plan (or `--plan` supplies one).
2. Every task starts the moment its dependencies finish, so each wave of
   independent tasks runs as a parallel team of workers. Workers can
   `delegate` further if you enabled it.
3. A verifier checks each finished task against its acceptance criteria.
   Rejected work goes back to the same worker, with its conversation and the
   feedback, up to `--max-retries` times. Tasks whose dependencies failed are
   skipped.
4. An integrator checks the whole goal, closes small gaps and writes the
   final report.

`--max-parallel` (default 8) caps concurrent model requests across the whole
team; tool execution does not hold a slot, so nested delegation cannot
deadlock.

## Models

`-m provider:model` or `DAEDALUS_AGENT_MODEL`. The default is
`anthropic:claude-opus-5-5`.

| Provider | Needs |
|---|---|
| `anthropic` | `ANTHROPIC_API_KEY`; optional `ANTHROPIC_BASE_URL` |
| `openai` | `OPENAI_API_KEY`; optional `OPENAI_BASE_URL` for any OpenAI-compatible server |

In `make agent`, add the API host (e.g. `api.anthropic.com`) to
`.daedalus/agent-allowlist.txt`.

## In a project

`daedalus.mk` wraps the three modes so they run inside the labyrinth fence:

```sh
make agent-ask       GOAL='why does molecule fail on rocky?' AGENT_TOOLS=read_file,grep
make agent-hyperplan GOAL='split the motd role'              AGENT_TOOLS=read_file,list_dir,glob,grep
make agent-ultrawork GOAL='split the motd role' \
    AGENT_TOOLS=read_file,list_dir,glob,grep,edit_file,write_file,run_bash
```

`AGENT_MODEL` and `AGENT_ARGS` pass `--model` and any other flags.

## Library use

```python
from pathlib import Path
from daedalus_agent.providers import build_provider
from daedalus_agent.runtime import Runtime
from daedalus_agent.team import ultrawork
from daedalus_agent.tools import ToolContext

provider, model = build_provider("anthropic:claude-opus-5-5")
runtime = Runtime(
    provider,
    model,
    enabled_tools=["read_file", "grep"],
    ctx=ToolContext(workspace=Path(".").resolve()),
)
result = await runtime.run_agent(name="main", prompt="...")
```

Adding a tool means writing a `ToolSpec` (a pydantic argument model plus an
async function) and listing it in `daedalus_agent/tools/__init__.py`. It is
still off until someone names it.

## Development

```sh
make agent-test        # from the repo root: pytest + ruff via uv
```

Python 3.12+ (tau's floor). `agent/requirements.txt` is the hashed lock the
image installs from; `make lock` regenerates it.
