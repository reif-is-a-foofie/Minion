# What the heck is this thing and why should I care about it?

Well, like me you are probably realizing by now that you have wasted a lot of your life reading slop from ChatGPT when you could have been making progress with Claude. 
I made my move to Claude recently and I realized, well, I have my whole life on GPT. That thing knows more about me than my doctor, my shrink and Siri combined times 6. 
And Claude doesn't know me from Adam other than it knows I'm not Adam, at least... most likely not Adam. 

In any case, OpenAI allows you to export all of yoru chats. It takes a few days. 
Then I needed to push that (context) back into Claude. 
But how?? I cant just load 30 million messages into Claude (real message count BTW, wild)
And given Claude spends more tokens than an kid at nickle-mania who just drank his first redbull....

I needed a solution. 

Naturally we go the encoding and semantic search route. Hook it up to an MCP that runs locally - so at run time, Claude can query my personal data and ONLY take in the context that matters. 

Its alpha, if you stretch alpha hard enough - lets be honest, bad things could happen. But at least I am committed to using my own software on my own personal set up. 

Ping me with questions - 

Reif

-reif@thegoodproject.net

## What you do with it

- Rebuild a usable “you” profile from your own writing (not hand-authored personality)
- Look up how you previously described something (projects, priorities, preferences) when you’re writing or deciding
- Keep an assistant consistent over time without stuffing long history into every chat

## Privacy

This repo is set up to avoid committing raw exports, derived embeddings, or personal quote banks.
You generate those locally when you need them.

## Quick start (high level)

1) Download your ChatGPT data export
2) Run the local tools to ingest + build memory
3) Generate the core profile from evidence
4) Use the profile + memory search in your assistant

Technical instructions live in `chatgpt_mcp_memory/README.md`.

