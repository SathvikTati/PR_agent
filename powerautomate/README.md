# Power Automate flow

The flow turns the Lambda payload into a Teams message with **real** @mentions.
A literal `@name` in message text is plain text and does not notify anyone, so
the flow must use the Teams connector's mention-token action to build the
mention HTML.

Build the flow once; adding repositories later is an S3 edit only.

## 1. Trigger — *When an HTTP request is received*

- Method: `POST`
- Request Body JSON Schema: paste the contents of [`trigger-schema.json`](trigger-schema.json).

Save the flow, then copy the generated **HTTP POST URL**. That URL is a secret
(it carries its own SAS signature) — it goes straight into AWS Secrets Manager
and nowhere else:

```bash
./scripts/set-secrets.sh --power-automate-url 'https://prod-00.westeurope.logic.azure.com:443/workflows/...'
```

## 2. Initialize variable — `MentionsHtml`

- Name: `MentionsHtml`
- Type: `String`
- Value: *(leave empty)*

## 3. Apply to each — over `reviewers`

Select an output from previous steps: `reviewers` (`@{triggerBody()?['reviewers']}`).

> In **Settings** for this loop, set **Concurrency Control = On, Degree of
> Parallelism = 1**. Appending to a variable from parallel branches drops
> mentions.

Inside the loop, two actions:

**3a. Microsoft Teams — *Get an @mention token for a user***

| Field | Value |
| --- | --- |
| User | `@{items('Apply_to_each')?['teams_email']}` |

This resolves the configured email to a genuine mention token
(`<at id="0">Display Name</at>`). If a reviewer's email is not a member of the
tenant the action fails — that is the signal that `repos.json` has a bad
address, not something to work around with plain text.

**3b. Append to string variable**

| Field | Value |
| --- | --- |
| Name | `MentionsHtml` |
| Value | `@{body('Get_an_@mention_token_for_a_user')} ` *(note the trailing space)* |

## 4. Compose — `MessageHtml`

Paste this as a single expression-bearing block (the `@{...}` parts are
expressions, the rest is literal HTML):

```html
<p>@{variables('MentionsHtml')} please review and merge this 🚀</p>
<p><b>@{if(equals(triggerBody()?['action'],'opened'),'New Pull Request',if(equals(triggerBody()?['action'],'reopened'),'Reopened Pull Request',if(equals(triggerBody()?['action'],'ready_for_review'),'Pull Request ready for review','Pull Request updated')))}: @{triggerBody()?['title']}</b></p>
<p><b>Repository:</b> @{triggerBody()?['repository']}</p>
<p><b>Branches:</b> @{triggerBody()?['source_branch']} → @{triggerBody()?['target_branch']}</p>
<p><b>Author:</b> @{triggerBody()?['author']}</p>
<p><b>Description:</b> @{if(empty(triggerBody()?['description']),'—',triggerBody()?['description'])}</p>
<p><b>PR link:</b> <a href="@{triggerBody()?['url']}">@{triggerBody()?['url']}</a></p>
```

## 5. Microsoft Teams — *Post message in a chat or channel*

| Field | Value |
| --- | --- |
| Post as | `Flow bot` |
| Post in | `Group chat` |
| Group chat | **pick the existing chat from the dropdown** |
| Message | `@{outputs('Compose')}` |

Pick the destination from the connector's picker rather than typing an ID. The
picker stores the chat's real conversation id (`19:...@thread.v2`); a display
name is not unique and a hand-written id will silently post nowhere useful.

To make the destination configurable without editing the flow, create a
Power Platform **environment variable** of type Text holding the chat id, and
bind the *Group chat* field to it. Promoting the flow between environments then
only changes the variable.

> The Flow bot must be a member of the group chat. If the post action returns
> `Forbidden`, add it to the chat (or switch **Post as** to a service account
> that is already a member).

## 6. Response — `200`

Add a **Response** action as the last step with status code `200` and body
`{ "status": "posted", "delivery_id": "@{triggerBody()?['delivery_id']}" }`.

Without it the trigger returns `202 Accepted` immediately, and the Lambda
cannot tell a successful post from a flow that failed halfway. With it, a flow
failure surfaces as a non-2xx response, the Lambda retries, and a persistent
failure shows up as a failed delivery in GitHub.

## Testing the flow on its own

```bash
curl -X POST "$POWER_AUTOMATE_URL" \
  -H 'Content-Type: application/json' \
  -d @powerautomate/sample-payload.json
```

A message should appear in the group chat with the reviewer highlighted and
notified.

## Resulting message

> **@Sathvik Tati** please review and merge this 🚀
>
> **New Pull Request: Add SSO authentication**
> **Repository:** CalfusApps/onboarding-services
> **Branches:** feature/sso → develop
> **Author:** sathvik
> **Description:** Adds SSO authentication.
> **PR link:** https://github.com/CalfusApps/onboarding-services/pull/123
