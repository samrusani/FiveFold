# 0027: Pre-registered external evaluation

Status: Proposed
Date: 2026-11-26
Spec sections affected on acceptance: Evaluator, Testing, Later releases
Other documents affected: `docs/plan.md`, `docs/studies.md`, `docs/conformance.md`

## Context

The [first study](../report.md) used one agent implementation, two
models and one public procurement scenario family. Its results motivate
three different checks. An external SDK should be able to use the
gateway. Model choice should be compared with a fixed agent framework.
Independent MCP servers should be able to run the lost-reply probe.

This record proposes three parts the maintainer can accept separately.
Record the accepted protocol before any measured run. Use the proposed
tag `v0.2.2a1` after the accepted scoring changes in 0025 and 0026 item 1
are in it. Record the tag, full commit, scenario digests, driver locks,
model IDs, server commits and price file in the run manifest. A different
tag or planned comparison needs a dated amendment before the first run.

## Part 1. Agent frameworks

### Choices

- **SDKs.** Choose three by public adoption and by their documented
  Python 3.12 and tool support. GitHub stars on 26 Nov 2026 provide a
  transparent selection measure. Stars measure interest, and repository
  scopes differ. [OpenAI Agents SDK, 30,889](https://github.com/openai/openai-agents-python)
  · [Google ADK, 22,364](https://github.com/google/adk-python)
  · [CrewAI, 61,306](https://github.com/crewAIInc/crewAI)
  · [Claude Agent SDK, 8,650](https://github.com/anthropics/claude-agent-sdk-python).
- **Model choice.** Compare each SDK on its favored model, or give each
  the same model. Use `gpt-6.1-sol` at the same supported effort and
  Standard rates in all three so the model is held fixed. Disable
  framework-supplied tools unrelated to FiveFold.
- **Pinning and location.** Use independent projects under
  `experiments/external-agents/` with one exact SDK requirement and a
  hash-pinned transitive lock and image per driver. The pre-release's
  source tree and manifest will identify the driver commit and lock.
  The FiveFold package retains its existing dependencies. The drivers
  and measurements wait for acceptance.

| Driver pin | G1 and G2 | B0 |
| --- | --- | --- |
| `openai-agents==0.26.0` | Use its local Streamable HTTP MCP client on the agent network. Use GPT-6.1 Sol through the OpenAI model adapter. | Use SDK function tools that send the direct simulator requests. [Release](https://github.com/openai/openai-agents-python/releases/tag/v0.26.0) · [MCP documentation](https://openai.github.io/openai-agents-python/mcp/) |
| `google-adk==2.15.1` | Use `McpToolset` with Streamable HTTP and route GPT-6.1 Sol through ADK's LiteLLM adapter in the driver lock. | Use ADK function tools for the direct requests. [Release](https://github.com/google/adk-python/releases/tag/v2.15.1) · [MCP source](https://github.com/google/adk-python/blob/v2.15.1/src/google/adk/tools/mcp_tool/mcp_toolset.py) · [LiteLLM adapter](https://adk.dev/agents/models/litellm/) |
| `crewai==1.15.45` | Use the SDK's Streamable HTTP MCP connection and its OpenAI model backend. Pin one agent and one task. | Use CrewAI tools for the direct requests. [Release](https://github.com/crewAIInc/crewAI/releases/tag/1.15.45) · [MCP documentation](https://docs.crewai.com/v1.15.45/en/mcp/overview) |

The B0 wrappers keep the tool names, fields and replies of the simulator
API. B0 alone gets the agent token for this run. Follow the
[split topology guide](../your-own-agent.md) for the connection and
credential boundary. A G1 or G2 driver gets the gateway endpoint and
the simulator stays outside the agent's network. Before a live run,
a synthetic preflight must show each driver can list and call the
existing tools and report using their current shapes. Stop and consult
the maintainer if a framework needs a shape or name change.

### Cells and costs

Use dev-001-single-po in B0, G1 and G2 at C0 through C3. Use seeds 7, 8
and 9. Select eight families per contract in advance, including
no-fault, F2, F4 request copy, F5 in-contract, F5 beyond deadline, F6
late, F7 late relevant price rise and F12 request loss. For F2 use the
answer hop, and for F4 and F12 use the destination request hop. In B0
this is the agent-simulator hop and in G1/G2 it is the gateway-simulator
hop. Use the committed fault variants and the split F5 headroom. Freeze
their exact split plans and digests in the preflight manifest before
running the studies. The 32 matched cells and three seeds give 288
episodes per SDK. This excludes fault points internal to the agent or
gateway. Treat SDK scheduling and split wall-time jitter as part of the
measured setup. Run the sequence in seeded blocks across SDK and
configuration to reduce drift effects.

Each SDK's conservative token estimate is **17.21 USD** at the GPT-6.1
Sol flat rates of 2 USD input and 10 USD output per million tokens, using
the higher-volume cohort described below. The GPT-6.1 Sol trace projects
7.36 USD per SDK. Allocate 100 USD to this part. Cap each SDK's full
study at 30 USD and each episode at 0.50 USD. The remaining 10 USD is headroom for a dated protocol amendment. The preflight remains
local and runs without a paid provider. [OpenAI Standard pricing](https://developers.openai.com/api/docs/pricing)

## Part 2. Models

Choose a fixed LangGraph agent in the in-process topology for all
models, or pair each model with its own recommended SDK. Use the
fixed agent and the same role prompts, tool surfaces, effort policy
and termination budgets, with vendor-specific settings declared
before running. Compare one flagship and one small model from each
of OpenAI, Anthropic and Google, plus two open-weight models from
the existing local pilot. A reduced model set is an option if an API
or the pre-registered spend bound makes the full design infeasible.
A changed model set requires an amended protocol before measured runs.

Use the 64 shared dev-001 fault cells from the original study, with
B0, G1 and G2 and seeds 7, 8 and 9. That is 576 episodes per model.
The estimated token volumes come from the 741-episode cohorts.
GPT-6.1 Sol counted 7,614,052 input and 370,962 output tokens, or
18.94 USD at the 17 Nov prices. Claude Sonnet 5.5 counted 17,775,041
input and 873,180 output tokens, or 44.28 USD. To plan headroom, use
the larger volume in each column scaled by 576/741. Rates are per
million tokens in USD. The planning price file date is 26 Nov 2026.
Use Standard, short-context rates and the effective rates on the study
date, including the announced Gemini 4 Carbon rates from 1 Dec.
Set a 60,000-token episode budget and stop earlier if the spend cap
would be exceeded.

| Model ID | Input / output USD | Estimated USD | Study cap USD |
| --- | ---: | ---: | ---: |
| `gpt-6.2-astra` | 8 / 40 | 137.69 | 150 |
| `gpt-6-luna` | 0.10 / 0.50 | 1.72 | 5 |
| `claude-fable-5-2` | 8 / 40 | 137.69 | 150 |
| `claude-haiku-5-5` | 0.10 / 0.50 | 1.72 | 5 |
| `gemini-4-carbon` | 4 / 20 | 68.84 | 80 |
| `gemini-3.5-flash-lite` | 0.30 / 2.50 | 5.84 | 10 |
| Qwen3-8B, local | 0 / 0 | 0 | 0 |
| Llama 3.1 8B Instruct, local | 0 / 0 | 0 | 0 |

The six paid caps sum to 400 USD. Cap an individual paid episode at
1.00 USD, with a remaining-charge check before each paid call. Abort
the study on unknown billed spend using the existing fail-closed
policy. The three parts have a combined proposed cap of 500 USD. These are token estimates. Track usage, billed cache
writes and any rate change. Recalculate the manifest from vendor
rates before spending and record a dated amendment if it affects
the planned runs. A seed pins the FiveFold harness plan. Treat it as a
model seed only when the provider supports it and the manifest names
the setting.

Sources checked on 26 Nov 2026. [OpenAI Standard pricing](https://developers.openai.com/api/docs/pricing)
· [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)
· [Claude models and pricing](https://platform.claude.com/docs/en/models/overview)
· [Gemini standard rates](https://ai.google.dev/gemini-api/docs/pricing)
· [Qwen3-8B model card and Apache 2.0 license](https://huggingface.co/Qwen/Qwen3-8B)
· [Llama 3.1 model card and community license](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct).
Use pinned local model content digests and the local runtime's version,
quantization and generation settings. Report local hardware and time
separately from the zero provider charge. Follow each model's
distribution terms.

## Part 3. MCP servers

Choose servers whose public source yields an isolated local build and
an observable durable effect, or include a test instance with the
maintainer's permission. Start with two source builds and synthetic
inputs in disposable instances. Use the Memory server's entity creation
and the Filesystem server's directory creation. In both cases the
probe can count the target object in a fresh namespace. A path or entity
name is the retry identity. An observed final count measures the durable
effect defined by the fixture. Publish that definition with the verdict.
The proposed sources share pinned revision
`c799c8552b0f27b7a33a7b9233773da144dddcf5`, verified on 26 Nov 2026.
[MCP Memory source](https://github.com/modelcontextprotocol/servers/tree/c799c8552b0f27b7a33a7b9233773da144dddcf5/src/memory)
· [MCP Filesystem source](https://github.com/modelcontextprotocol/servers/tree/c799c8552b0f27b7a33a7b9233773da144dddcf5/src/filesystem).

Choose the two cases a normal instance can run, or add the pre-commit
case with a cooperating fixture. Start with reply lost after commit and
duplicate delivery, three fresh namespaces per case at seeds 7, 8 and 9.
The harness drops replies. Timeout before commit needs trusted fixture
hooks, so mark it unavailable for instances without them. Pin the
transport, fixture and probe before execution. Build from the checked
source and keep the lock and image digest. Use no account, private data
or hosted production service for this candidate set. Give the maintainers
the results 14 days before publication, and publish their answer with
the results. Record whether a reply arrived. The repository's
[conformance guide](../conformance.md) defines evidence and verdicts.

## Analysis and publication

- **Primary measure.** For each framework or model and configuration,
  report the share of scored trials on the shared cells that pass all
  five properties. Include `pass_unresolved` and report it separately.
  Report numerators, planned and scored denominators, infrastructure
  failures and inconclusive runs. A paired comparison uses blocks
  complete in both configurations. Also show a sensitivity result with
  missing outcomes counted as failures.
- **Intervals.** For Parts 1 and 2 use a paired cluster bootstrap
  over fault cells, including all three seeds and their paired
  configurations together in each draw. Use 10,000 resamples with
  analysis seed 2701 and percentile 95% intervals for rates and
  paired differences. The intervals describe variation across these
  cells. They support no claim about a sampled population of tasks.
- **Comparisons.** Report G2 minus B0 and G2 minus G1 per framework and per
  model. Compare the three frameworks pairwise within each configuration
  with GPT-6.1 Sol fixed. Compare flagship versus small within each lab
  and configuration. Report open-weight results alongside the other
  models with the same denominators. Report all planned comparisons in
  their order. Do not rank models from single comparisons outside the
  plan.
- **MCP.** Use the pass rate on shared conformance cases across servers
  and seeds as the primary summary for Part 3. Report each server and
  case separately and compare the matched cases across servers.
  Give exact counts and two-sided 95% Wilson intervals as descriptive
  summaries. Report unavailable cases and inconclusive evidence with
  their own counts. Treat the two source builds as case studies.
- **Deviations.** Log a dated entry naming the plan, reason,
  affected trials and impact. Keep the original denominators and
  results. Label additional analyses exploratory. Stop for a maintainer
  decision on a material change before collecting replacement results.

Publish cassettes, the dated rates, manifests, raw results and analysis
scripts with content digests alongside the tagged release. Ship only
shareable prompts and responses. Remove credentials and incidental
personal identifiers before recording or publication. If a provider
or held-out restriction prevents release, report the missing artifact
and the reason. Keep externally held-out scenarios entirely outside the
build team's workflow. Record 0028 and a separately commissioned held-out
set can later address the current one-public-family limit.

Framework and model versions can drift. Compare only the pinned
manifest, publish version changes, and keep later runs distinct.
Live models and the split topology are nondeterministic. Three seeds
characterize variation on this setup; the public scenario family, tool
prompt and selected services limit generalization. Token estimates
from two older models are forecasts, and billed usage is the measure
of actual spend.

## Disclosure

Record 0023 describes the maintainer's interest in AliceMemory and this
repository's optional AliceMemory adapter exercise. AliceMemory is
outside the proposed evaluation. The maintainer adds any other relevant
interests before accepting this record.
