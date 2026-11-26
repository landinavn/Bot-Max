"""Safe domain errors; never carry provider responses, prompts or credentials."""
class EntitlementError(Exception): pass
class InvalidOperation(EntitlementError): pass
class Conflict(EntitlementError): pass
class NotFound(EntitlementError): pass
class InsufficientRights(EntitlementError): pass
class InvalidTransition(EntitlementError): pass
class LegacySchemaError(EntitlementError): pass
class UnverifiedLink(EntitlementError): pass
