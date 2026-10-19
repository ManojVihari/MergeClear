import json


class MarkdownBuilder:

    def build(self, route_data, llm_sections):

        # 🔥 SAFE NAME
        api_name = getattr(route_data, "function_name", None) or getattr(route_data, "function", "unknown")

        title = llm_sections.get("title") or api_name
        md = f"# {title}\n\n"

        # =========================
        # 🔥 Overview
        # =========================
        md += "## Overview\n"
        md += llm_sections.get("overview", "No overview available.") + "\n\n"

        # =========================
        # 🔥 Endpoint
        # =========================
        md += "## Endpoint\n"
        md += f"- **API id:** `{getattr(route_data, 'doc_name', None) or getattr(route_data, 'handler', None) or api_name}`\n"
        md += f"- **Method:** {route_data.method}\n"
        md += f"- **Path:** `{route_data.path}`\n"

        context_path = getattr(route_data, "context_path", None)
        if context_path:
            md += f"- **Context path:** `{context_path}`\n"

        handler = getattr(route_data, "handler", None)
        if handler:
            md += f"- **Handler:** `{handler}`"
            source_file = getattr(route_data, "file", None)
            if source_file:
                md += f" ({source_file}:{getattr(route_data, 'line', '')})"
            md += "\n"

        for label, key in (("Consumes", "consumes"), ("Produces", "produces")):
            values = getattr(route_data, key, None)
            if values:
                md += f"- **{label}:** {', '.join(values)}\n"

        if getattr(route_data, "deprecated", False):
            md += "- **Deprecated:** yes\n"

        md += "\n"

        description = getattr(route_data, "description", None)
        if description:
            md += f"> {description}\n\n"

        md += self._security(route_data)

        # =========================
        # 🔥 Request (DTO Structured)
        # =========================
        parameters = getattr(route_data, "parameters", None) or getattr(route_data, "params", [])

        if parameters:
            md += "## Request\n\n"

            for param in parameters:

                name = getattr(param, "name", param.get("name"))
                ptype = getattr(param, "type", param.get("type"))
                schema = getattr(param, "schema", param.get("schema", {}))

                md += f"### {name} ({ptype})\n\n"

                if schema:
                    md += "| Field | Type | Required | Validation |\n"
                    md += "|------|------|----------|------------|\n"

                    for field, details in schema.items():
                        ftype = details.get("type")
                        validation = details.get("validation", {})

                        rules = ", ".join(validation.keys()) if validation else "-"
                        req = "Yes" if validation.get("required") else "No"

                        md += f"| {field} | {ftype} | {req} | {rules} |\n"

                md += "\n"

        # =========================
        # 🔥 Response (FIXED)
        # =========================
        response = getattr(route_data, "response", {})

        md += "## Response\n\n"

        if response.get("type"):
            md += f"- **Type:** {response.get('type')}\n"

        # 🔥 Prefer structured schema
        if isinstance(response.get("schema"), dict):
            md += "- **Schema:**\n"
            md += "```json\n"
            md += json.dumps(response["schema"], indent=2)
            md += "\n```\n\n"

        else:
            # 🔥 DO NOT show internal method call
            desc = llm_sections.get("response_description")

            if desc:
                md += f"- **Description:** {desc}\n\n"
            else:
                md += "- **Description:** Returns result of operation\n\n"

        # =========================
        # 🔥 Errors
        # =========================
        errors = getattr(route_data, "errors", None) or []

        if errors:
            md += "## Errors\n\n"
            md += "| Code | Field | Rule |\n"
            md += "|------|------|------|\n"

            for err in errors:
                if not isinstance(err, dict):
                    # plain status code (404) or HttpStatus name (NOT_FOUND)
                    md += f"| {err} | - | - |\n"
                    continue

                fields = err.get("fields", [])

                for f in fields:
                    md += f"| {err.get('status')} | {f.get('name')} | {f.get('rule')} |\n"

            md += "\n"

        md += self._status_codes(route_data)

        # =========================
        # 🔥 Process Flow (KEY UPGRADE)
        # =========================
        flow = llm_sections.get("business_flow", [])

        if not flow:
            # fallback using AST calls
            direct = getattr(route_data, "call_graph", {}).get("direct", [])
            flow = [f"Invoke {c}" for c in direct]

        if flow:
            md += "## Process Flow\n"
            for i, step in enumerate(flow, 1):
                md += f"{i}. {step}\n"
            md += "\n"

        # =========================
        # 🔥 DB Operations
        # =========================
        db_ops = getattr(route_data, "db_ops", [])

        if db_ops:
            md += "## Database Operations\n"
            for op in db_ops:
                md += f"- **{op.get('type')}** → `{op.get('call')}`\n"
            md += "\n"

        md += self._integrations(route_data)

        # =========================
        # 🔥 Business Logic
        # =========================
        md += "## Business Logic\n"
        md += llm_sections.get("business_logic", "No details available.") + "\n\n"

        # =========================
        # 🔥 Breaking Changes
        # =========================
        breaking = getattr(route_data, "breaking_changes", [])

        if breaking:
            md += "## Breaking Changes\n"
            for b in breaking:
                md += f"- {self._breaking_change(b)}\n"
            md += "\n"

        # =========================
        # 🔥 Change Impact
        # =========================
        md += "## Change Impact\n"
        md += llm_sections.get("change_impact", "No impact details available.") + "\n"

        return md

    # =========================
    # 🔥 Sections built from the Spring application model
    # =========================

    @staticmethod
    def _breaking_change(b):
        where = f"`{b['dto']}.{b.get('field')}`" if b.get("dto") else f"`{b.get('field')}`"
        kind = b.get("type")

        if kind == "FIELD_ADDED":
            return f"**{kind}** → {where} ({b.get('new_type')}{', required' if b.get('required') else ''})"
        if kind == "FIELD_REMOVED":
            return f"**{kind}** → {where} (was {b.get('old_type')})"
        if kind == "TYPE_CHANGED":
            return f"**{kind}** → {where}: {b.get('old_type')} → {b.get('new_type')}"
        if kind == "VALIDATION_CHANGED":
            return f"**{kind}** → {where} {b.get('rule')}: {b.get('old')} → {b.get('new')}"
        return f"**{kind}** → {where} ({b.get('rule')})"

    @staticmethod
    def _security(route_data):
        security = getattr(route_data, "security", None)
        if not security:
            return ""

        md = "## Security\n"
        for a in security.get("annotations") or []:
            name = a.get("annotation")
            md += f"- `{name}`" if name in ("Depends", "Security") else f"- `@{name}`"
            if a.get("expression"):
                md += f" `{a['expression']}`"
            md += f" ({a.get('level')})\n"

        rule = security.get("url_rule")
        if rule:
            roles = f" {', '.join(rule['roles'])}" if rule.get("roles") else ""
            md += f"- URL rule `{', '.join(rule.get('patterns', []))}` → **{rule.get('access')}**{roles}"
            if rule.get("defined_in"):
                md += f" (in `{rule['defined_in']}`)"
            md += "\n"

        return md + "\n"

    @staticmethod
    def _status_codes(route_data):
        codes = getattr(route_data, "status_codes", None) or []
        rows = []

        for status in codes:
            data = status.model_dump() if hasattr(status, "model_dump") else dict(status)
            source = data.get("source") or data.get("detail") or ""
            rows.append(f"| {data.get('code')} | {data.get('reason') or ''} | {source} |")

        if not rows:
            return ""

        md = "## Status Codes\n\n| Code | Reason | Source |\n|------|------|------|\n"
        return md + "\n".join(rows) + "\n\n"

    @staticmethod
    def _integrations(route_data):
        integrations = getattr(route_data, "integrations", None) or {}
        lines = []

        for f in integrations.get("databases", []):
            tables = ", ".join(f.get("table") or []) or f.get("entity") or "?"
            lines.append(f"- **Database** ({f.get('technology')}): {f.get('operation')} `{tables}` via `{f.get('call') or f.get('source')}`")

        for f in integrations.get("caches", []):
            target = ", ".join(f.get("cache_names") or []) or f.get("key") or ""
            lines.append(f"- **Cache** ({f.get('technology')}): {f.get('operation')} `{target}` in `{f.get('source')}`")

        for f in integrations.get("messaging", []):
            lines.append(f"- **Messaging** ({f.get('technology')}): {f.get('direction')} to `{f.get('destination')}`"
                         + (f" ({f['payload_type']})" if f.get("payload_type") else "") + f" in `{f.get('source')}`")

        for f in integrations.get("external_apis", []):
            target = f.get("target_service") or f.get("host") or ""
            url = f.get("url") or ""
            lines.append(f"- **HTTP call** ({f.get('client')}): {f.get('http_method') or ''} `{url}`"
                         + (f" → {target}" if target else "") + f" in `{f.get('source')}`")

        for f in integrations.get("events", []):
            lines.append(f"- **Event**: publishes `{f.get('event_type')}` in `{f.get('source')}`")

        for group in ("storage", "email", "search"):
            for f in integrations.get(group, []):
                detail = f.get("bucket") or f.get("call") or ""
                lines.append(f"- **{group.title()}** ({f.get('technology')}): `{detail}` in `{f.get('source')}`")

        if not lines:
            return ""

        return "## Dependencies & Integrations\n" + "\n".join(lines) + "\n\n"
