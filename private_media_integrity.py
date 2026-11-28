"""Strict S3 integrity metadata. Normalize field names, never digest values."""
import re


class S3IntegrityError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


def metadata_sha256(metadata):
    if metadata is None:raise S3IntegrityError('metadata_missing')
    if not isinstance(metadata,dict):raise S3IntegrityError('metadata_invalid')
    if any(not isinstance(k,str) or not isinstance(v,str) for k,v in metadata.items()):
        raise S3IntegrityError('metadata_invalid')
    matches=[value for key,value in metadata.items() if key.lower()=='sha256']
    if not matches:raise S3IntegrityError('metadata_missing')
    if len(matches)!=1:raise S3IntegrityError('metadata_duplicate_sha256')
    value=matches[0]
    if not re.fullmatch(r'[0-9a-fA-F]{64}',value):raise S3IntegrityError('metadata_invalid_sha256')
    return value


def verify_s3_integrity(response,size,digest):
    if not isinstance(response,dict):raise S3IntegrityError('metadata_missing')
    if type(response.get('ContentLength')) is not int or response['ContentLength']!=size:
        raise S3IntegrityError('size_mismatch')
    actual=metadata_sha256(response.get('Metadata'))
    if actual!=digest:raise S3IntegrityError('sha256_mismatch')
