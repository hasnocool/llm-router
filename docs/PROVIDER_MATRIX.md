# Provider/program matrix

This snapshot is packaged as four versionable chunks at `src/llm_router/data/provider_matrix_*.json`. The runtime only attempts providers configured in `config.toml`; the wider matrix is retained so new adapters can be added without redesigning the scoring schema.

Snapshot: **2026-08-10** · **52 entries** · 28 recurring · 16 trial · 8 conditional.

The routing score is a zero-cost automation heuristic, **not a model benchmark**. `tok/day eq.` is populated only when a published token quota can be normalized without inventing a conversion.

| Score | Provider / program | Class | Cadence | Access | Router | tok/day eq. | Coding | OpenAI | Tools | Vision | Card |
|---:|---|---|---|---|---|---:|---:|---|---|---|---|
| 94 | **GroqCloud** | recurring | daily | API | direct | 500,000 | 4.3 | yes | model-dependent | model-dependent | no |
| 90 | **Kilo Gateway / Auto Free** | recurring | hourly | API | direct | — | 4.5 | yes | yes | model-dependent | no |
| 89 | **Lightning AI Free** | recurring | monthly | compute/API | direct | — | 4.2 | yes | yes | model-dependent | no |
| 88 | **Mistral API / Studio Free mode** | recurring | monthly | API | direct | — | 4.5 | yes | yes | model-dependent | no |
| 85 | **GitHub Models** | recurring | daily | API | direct | — | 4.5 | yes | model-dependent | model-dependent | no |
| 85 | **OpenRouter** | recurring | daily | API | direct | — | 4.6 | yes | model-dependent | model-dependent | no |
| 85 | **Vercel AI Gateway** | recurring | monthly | API | direct | — | 4.6 | yes | model-dependent | model-dependent | no |
| 84 | **Hugging Face Inference Providers** | recurring | monthly | API | direct | — | 4.2 | yes | model-dependent | model-dependent | no |
| 83 | **Google Gemini API / AI Studio** | recurring | daily | API | direct | — | 4.8 | partial | yes | yes | no |
| 81 | **IBM watsonx.ai Runtime Lite** | recurring | monthly | API | direct | 10,000 | 4.0 | unknown | model-dependent | model-dependent | no |
| 80 | **OpenCode Zen Free Models** | recurring | limited-time | API/CLI | direct | — | 4.6 | yes | model-dependent | model-dependent | conditional |
| 79 | **Cloudflare Workers AI** | recurring | daily | API | direct | — | 4.0 | partial | model-dependent | model-dependent | no |
| 79 | **Cohere** | recurring | monthly | API | direct | — | 4.1 | unknown | yes | model-dependent | no |
| 73 | **Google Antigravity Individual** | recurring | weekly | CLI/IDE | indirect | — | 4.8 | no | yes | model-dependent | no |
| 70 | **Amazon Q Developer Free** | recurring | monthly | CLI/IDE | indirect | — | 4.3 | no | yes | unknown | no |
| 70 | **BytePlus ModelArk Data Collaboration Rewards** | conditional | daily | API | direct | 5,000,000 | 4.6 | partial | model-dependent | model-dependent | no |
| 70 | **Modal Starter** | recurring | monthly | compute/API hosting | self_host | — | 4.0 | partial | self-hosted | self-hosted | no |
| 69 | **Cerebras Inference** | trial | one-time | API | direct | 1,000,000 | 4.6 | yes | model-dependent | model-dependent | yes |
| 68 | **Cursor Hobby** | recurring | monthly | IDE | indirect | — | 4.5 | no | yes | model-dependent | no |
| 68 | **GitHub Codespaces Included Usage** | recurring | monthly | compute/CLI | self_host | — | 3.5 | partial | self-hosted | self-hosted | no |
| 68 | **GitHub Copilot Free** | recurring | monthly | CLI/IDE | indirect | — | 4.5 | no | partial | model-dependent | no |
| 67 | **Windsurf Free** | recurring | dynamic | IDE | indirect | — | 4.2 | no | yes | model-dependent | no |
| 66 | **Replit Starter / Agent** | recurring | daily | web IDE | indirect | — | 4.0 | no | yes | model-dependent | no |
| 65 | **Google Colab Free** | recurring | dynamic | compute | self_host | — | 3.8 | partial | self-hosted | self-hosted | no |
| 65 | **Kaggle Notebooks GPU** | recurring | weekly | compute | self_host | — | 3.8 | partial | self-hosted | self-hosted | no |
| 65 | **Oracle Cloud Always Free Compute** | recurring | monthly | compute | self_host | — | 3.4 | partial | self-hosted | self-hosted | conditional |
| 64 | **GitHub Actions Included Minutes** | recurring | monthly | batch compute | self_host | — | 2.5 | partial | self-hosted | self-hosted | no |
| 64 | **Google Cloud Free Tier** | recurring | monthly | compute/cloud | self_host | — | 3.2 | partial | self-hosted | self-hosted | conditional |
| 64 | **SambaNova Cloud** | trial | one-time | API | direct | — | 4.5 | yes | model-dependent | model-dependent | no |
| 61 | **JetBrains AI Free** | recurring | 30-days | IDE | indirect | — | 4.0 | no | partial | unknown | no |
| 60 | **Alibaba Cloud Model Studio (International/Singapore)** | trial | one-time | API | direct | — | 4.6 | yes | model-dependent | yes | unknown |
| 60 | **BytePlus ModelArk New-User Free Tokens** | trial | one-time | API | direct | — | 4.4 | partial | model-dependent | model-dependent | no |
| 60 | **Hyperbolic** | trial | one-time | API | direct | — | 4.2 | yes | model-dependent | model-dependent | conditional |
| 59 | **Fireworks AI** | trial | one-time | API | direct | — | 4.5 | yes | model-dependent | model-dependent | unknown |
| 59 | **Hugging Face ZeroGPU Free** | recurring | daily | Space/API endpoint | indirect | — | 3.8 | no | space-dependent | space-dependent | no |
| 59 | **Scaleway Generative APIs Free Tier** | trial | one-time | API | direct | — | 4.4 | yes | model-dependent | model-dependent | unknown |
| 57 | **Baseten Kimi K3 Model Drop Credits** | trial | one-time | API | direct | — | 4.8 | partial | model-dependent | yes | unknown |
| 57 | **NVIDIA API Catalog / hosted NIM trial endpoints** | trial | one-time/limited | cloud | direct | — | 4.4 | partial | model-dependent | model-dependent | conditional |
| 57 | **OVHcloud AI Endpoints Batch API Beta** | trial | temporary-beta | API | direct | — | 4.0 | yes | model-dependent | model-dependent | unknown |
| 57 | **Together AI Startup Accelerator** | conditional | program | cloud | direct | — | 4.6 | yes | model-dependent | model-dependent | unknown |
| 55 | **Baseten for Startups** | conditional | program | API/compute | direct | — | 4.4 | partial | model-dependent | model-dependent | conditional |
| 45 | **AWS Free Tier credits (including eligible Amazon Bedrock use)** | trial | one-time | cloud | self_host | — | 4.2 | partial | service-dependent | service-dependent | unknown |
| 45 | **Azure for Students** | conditional | annual | cloud | self_host | — | 3.5 | partial | service-dependent | service-dependent | no |
| 45 | **Microsoft Azure Free Account** | trial | one-time | cloud | self_host | — | 4.2 | partial | service-dependent | service-dependent | yes |
| 44 | **Modal Academic Credits** | conditional | program | compute | self_host | — | 4.0 | partial | self-hosted | self-hosted | conditional |
| 44 | **Modal for Startups** | conditional | program | compute | self_host | — | 4.0 | partial | self-hosted | self-hosted | conditional |
| 44 | **Oracle Cloud Free Trial** | trial | one-time | compute/cloud | self_host | — | 3.5 | partial | self-hosted | self-hosted | conditional |
| 43 | **AWS Activate Credits** | conditional | program | cloud | self_host | — | 4.2 | partial | service-dependent | service-dependent | unknown |
| 43 | **Azure for Startups** | conditional | program | cloud | self_host | — | 3.8 | partial | service-dependent | service-dependent | conditional |
| 42 | **Windsurf Pro Trial** | trial | one-time | IDE | indirect | — | 4.6 | no | yes | model-dependent | unknown |
| 41 | **Google Cloud Free Trial** | trial | one-time | cloud | self_host | — | 3.2 | partial | self-hosted | self-hosted | yes |
| 36 | **JetBrains AI Trial** | trial | one-time | IDE | indirect | — | 4.3 | no | partial | unknown | yes |

## Router eligibility summary

- `direct`: **26**
- `indirect`: **10**
- `self_host`: **16**

Runtime JSON retains the routing-critical fields (quota equivalent, capabilities, region/privacy/commercial-use policy, quota endpoint, eligibility, score, verification date, and notes). The wider research catalog remains the upstream source for long-form allowance/source provenance.
