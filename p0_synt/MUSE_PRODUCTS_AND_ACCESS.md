# Personal Meta Muse, Muse Code and OpenAI Dots — distinct products

## Personal Muse (Meta)
- Personal Muse, announced 8 September 2026, is a cloud AI agent using a secure VM with browser. US rollout on iOS, Android, and https://muse.ai; Meta publishes a Mac desktop download. A Windows muse.cmd executable is NOT Personal Muse.
- Native provisioning requires account-side access and a successful authenticated run. We did not obtain that access.
- Once available, name an external worker Synt-P0-Research and give it verified-research-only tasks; do not duplicate the P0 master. Require DOI/URL, actual prototype, limitations, negative results.

## Muse Code (developer CLI)
- Installed Muse Code 1.4.3 on Windows and Ubuntu WSL; project skill validation passed, and echo-provider smoke passed without live inference.
- Live Meta provider blocked: missing credentials; interactive muse login failed with device-flow transport error.
- A bounded workflow draft is workflow_synt.js. Not yet imported with muse workflows save and not yet executed. Only import after local connectivity and account authorization, confirming project zero-spend constraints.
- When authorized: muse workflows save p0-synt --from workflow_synt.js --scope project; muse workflows list; then run one bounded live workflow in the authorized project.

## Native ChatGPT Dots
- Dots are a ChatGPT cloud product; provisioning cannot be performed by local CLI, GitHub or Muse Code.
- Dots rollout: eligible Pro accounts outside the EEA, UK, Switzerland; Business Premium supported regions; Enterprise/Edu/Healthcare beta opt-in. Native account eligibility not confirmed.
- If Dot setup is available, create Synt-P0-Reviewer in ChatGPT web/desktop, paste DOT_BOOTSTRAP.md, restrict permissions, connect relevant resources and verify a real completed task.

References:
https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/
https://dev.meta.ai/docs/muse-code
https://help.openai.com/en/articles/20001530-getting-started-with-your-dot
