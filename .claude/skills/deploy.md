# /deploy — Commit, push & deploy

When the user invokes this skill, follow these steps exactly:

## Step 1 — Check what changed
Run in parallel:
- `git status` to see untracked and modified files
- `git diff` to see the actual changes

## Step 2 — Draft a commit message
Analyze the diff and write a concise commit message (1-2 sentences) that describes WHAT changed and WHY. Follow the existing commit style in this repo.

If the user provided a message after `/deploy` (e.g. `/deploy fix image generation`), use that as the commit message instead.

## Step 3 — Confirm with the user
Show the user:
- List of files that will be committed
- The proposed commit message

Ask: "Закоммитить и запушить? (да / нет / изменить сообщение)"

## Step 4 — Commit and push
After confirmation:
1. `git add` the relevant files (prefer specific files over `git add .`, but if all changes should be committed, use `git add -A`)
2. `git commit -m "..."` with the approved message + Co-Authored-By trailer
3. `git push`

## Step 5 — Report
Tell the user:
- Commit hash
- That GitHub Actions will automatically deploy to Fly.io (usually takes 2-3 minutes)
- Link to check the deploy: https://github.com/Scropy666/ai_news_telegram_channel/actions
