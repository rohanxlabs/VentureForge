---
title: VentureForge - AI Startup Discovery
emoji: 🚀
colorFrom: blue
colorTo: purple
sdk: gradio
sdk_version: 6.14.0
app_file: app.py
python_version: '3.13'
pinned: false
tags:
  - amd-hackathon-2026
  - ai-agents
  - startup-ideas
  - multi-agent
  - paul-graham
short_description: Autonomous multi-agent system for startup idea discovery
---

# VentureForge 🚀

An autonomous multi-agent system that discovers startup ideas by mining real user pain points from online communities, evaluating them with Paul Graham-inspired criteria, and generating investor-ready pitch briefs.

**Built for AMD AI Hackathon | Track 1: AI Agents & Agentic Workflows**

## Features

- 🔍 **Pain Point Mining** from Hacker News, Product Hunt, and web search
- 🤖 **6 Specialized Agents** (Orchestrator, Miner, Generator, Scorer, Writer, Critic)
- ✅ **Binary Rubric Evaluation** (yes/no checks, not subjective scores)
- 🔄 **Reflection Loop** with adversarial critique (max 3 revisions)
- 📊 **Real-time Progress** tracking in Gradio UI
- 🎯 **Token Optimized** for vLLM 2048 token limit

## How to Use

1. **Set API Keys** in Space Settings → Repository secrets:
   - `YOUTUBE_API_KEY` (required)
   - `PRODUCTHUNT_API_KEY` (optional)
  ` (optional)

1. **Enter a domain** (e.g., "developer tools", "healthcare", "fintech")

2. **Click "Start Discovery"** and watch the agents work!

3. **View results** in the tabs:
   - Pain Points (with evidence sources)
   - Ideas (generated from pain points)
   - Scored Ideas (8 binary checks)
   - Pitch Briefs (investor-ready)
   - Critiques (adversarial review)

## Architecture

```
Orchestrator → Pain Point Miner → Idea Generator → Scorer → Pitch Writer → Critic
                                                                              ↓
                                                                    (reflection loop)
```


## Performance

- **Duration:** ~2 minutes per domain
- **Token Optimized:** One-at-a-time generation + compressed prompts
- **Reliable:** 0 JSON failures on all domains (healthcare, developer tools, finance)

## License

MIT License - see [LICENSE](LICENSE) file for details

---

**Built with ❤️ for AMD AI Hackathon**
