---
name: investigate
description: "Deep bug investigation. Pass a short description of the problem — error message, unexpected behavior, or failing test. Returns structured root cause analysis with evidence and fix recommendation. Runs only when the user explicitly types /dynos-work:investigate; never auto-triggered from conversation or by reported bugs."
---

# dynos-work: investigate

Spawn the `investigator` agent with the user's prompt as the instruction.

## What to pass

Pass the user's full prompt verbatim as the instruction to the agent. Do not summarize or reformat it.

## Usage

```
/dynos-work:investigate <your problem description>
```

Examples:
```
/dynos-work:investigate TypeError: Cannot read properties of undefined reading 'id' at UserService.ts:47
/dynos-work:investigate the checkout flow always skips the discount calculation when coupon code is applied
/dynos-work:investigate test suite: AuthController > login > should return 401 on invalid password is failing
```
