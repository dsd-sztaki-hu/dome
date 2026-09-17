<!-- ************************************************************************************************
Copyright (C) 2025-2026 SZTAKI, Department of Distributed Systems ([https://dsd.sztaki.hu](https://dsd.sztaki.hu)).

SPDX-License-Identifier: Apache-2.0
************************************************************************************************ -->
# Example deployment-specific DOME instructions

These instructions are added to DOME's built-in guidance and are sent to the
MCP client during initialization. Adapt them to the policies of your
Dataverse installation.

## Dataset workflow

- Use draft operations for dataset creation and editing.
- Do not publish or release a dataset automatically after creating or editing
  it.
- Publish only when the user explicitly asks for publication, or explicitly
  confirms publication after you explain that it is the next step.
- Treat requests such as "create a dataset", "edit the dataset", "save it",
  or "prepare it" as draft-only requests.
- Do not treat an earlier message or an inferred preference as confirmation.

## Confirmation

Before an explicit publication request is executed, briefly explain that the
action changes the dataset's release state and ask the user to confirm the
target dataset and version when they have not already specified them.

## Local conventions

- Ask for a dataverse alias or identifier when the destination is ambiguous.
- Use the tool descriptions and schemas supplied by DOME; do not invent
  operation names or request fields.
- Never include Dataverse API tokens or other credentials in tool arguments or
  user-facing responses.
