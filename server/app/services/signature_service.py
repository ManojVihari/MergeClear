import hashlib
import json


class SignatureService:

    def generate(self, route):

        contract = self._normalize(route)

        contract_str = json.dumps(contract, sort_keys=True)

        return hashlib.md5(contract_str.encode()).hexdigest()

    def _normalize(self, route):

        """
        Convert route object → stable dict covering the API contract:
        method, path, parameters and error codes.
        """

        # The scanner sends `params`/`errors`; `parameters` is the older name.
        # Only status *codes* are contract; their detected source is not.
        parameters = [self._contract_parameter(p) for p in (route.parameters or route.params or [])]
        codes = sorted({s.code for s in (route.status_codes or [])})

        contract = {
            "method": route.method,
            "path": route.path,
            "parameters": self._sorted(parameters),
            "status_codes": self._sorted(list(route.errors or []) + codes)
        }

        # the response body is contract too (only added when the scanner sends it,
        # so routes without response data keep their existing signatures)
        response = route.response or {}
        if response.get("body_type") or response.get("schema"):
            contract["response"] = {"body_type": response.get("body_type"), "schema": response.get("schema")}

        return contract

    # which dependency supplies a parameter, and its description, are documentation, not contract
    NON_CONTRACT_PARAMETER_KEYS = ("via", "description")

    def _contract_parameter(self, parameter):
        if isinstance(parameter, dict):
            return {k: v for k, v in parameter.items() if k not in self.NON_CONTRACT_PARAMETER_KEYS}
        return parameter

    def _sorted(self, items):
        normalized = [self._normalize_obj(i) for i in items]
        return sorted(normalized, key=lambda x: json.dumps(x, sort_keys=True, default=str))

    def _normalize_obj(self, obj):

        """
        Converts Pydantic object → dict safely and consistently.
        Plain values (status codes, dicts) are returned as-is.
        """

        if hasattr(obj, "model_dump"):
            return obj.model_dump()

        if hasattr(obj, "dict") and not isinstance(obj, dict):
            return obj.dict()

        return obj
