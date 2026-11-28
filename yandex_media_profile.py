"""Opt-in, exact Yandex policy profile. No IAM/configuration mutations.

An empty ACL proves only that no ACL grants were returned. Administrative
identity/public-access/IAM checks cannot be inferred from these S3 responses.
"""
import json
import re


def refuse():
    from private_media import MediaUnavailable
    raise MediaUnavailable('Yandex private bucket profile was not verified')


def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:refuse()
        result[key]=value
    return result


def items(value):
    values=value if isinstance(value,list) else [value]
    if not values or any(not isinstance(v,str) or not v for v in values):refuse()
    if len(values)!=len(set(values)):refuse()
    return frozenset(values)


def conditions(value):
    if value is None:return {}
    if not isinstance(value,dict):refuse()
    result={};seen=set()
    for operator,entries in value.items():
        if operator!='Bool' or not isinstance(entries,dict) or not entries:refuse()
        normalized={}
        for key,setting in entries.items():
            if not isinstance(key,str):refuse()
            key=key.lower()
            if key in seen or key!='aws:securetransport':refuse()
            seen.add(key)
            if type(setting) is bool:setting='true' if setting else 'false'
            if setting not in ('true','false'):refuse()
            normalized[key]=setting
        result[operator]=normalized
    return result


def validate_configuration(config):
    """Operator assertions are explicit, fail closed and are not remote proof."""
    if config.private_media_profile!='yandex-v1':refuse()
    if config.storage_endpoint_url not in ('https://storage.yandexcloud.net','https://storage.yandexcloud.net/'):refuse()
    if not config.private_media_bucket or config.private_media_bucket==config.storage_bucket:refuse()
    folder=config.private_media_yandex_folder_id
    runtime=config.private_media_runtime_principal
    allowed=config.private_media_principals
    legacy=config.private_media_legacy_principals
    if (not folder or not runtime or len(allowed)!=2 or runtime not in allowed
            or len(legacy)!=2 or allowed&legacy or folder in allowed or folder in legacy
            or config.private_media_yandex_controls_verified is not True):refuse()
    if any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',v)
           for v in (folder,runtime,*allowed,*legacy)):refuse()


def validate_acl(acl,folder):
    if (not isinstance(acl,dict) or set(acl)-{'Owner','Grants','ResponseMetadata'}
            or not isinstance(acl.get('Owner'),dict) or not isinstance(acl.get('Grants'),list)):
        refuse()
    owner=acl['Owner']
    if (set(owner)-{'ID','DisplayName'} or 'ID' not in owner or type(owner['ID']) is not str
            or owner['ID'] not in ('',folder)
            or ('DisplayName' in owner and not isinstance(owner['DisplayName'],str))
            or acl['Grants']!=[]):refuse()
    if 'ResponseMetadata' in acl and not isinstance(acl['ResponseMetadata'],dict):refuse()
    if acl.get('ResponseMetadata',{}).get('HTTPStatusCode',200)!=200:refuse()


def validate_policy(raw,config):
    if not isinstance(raw,dict) or not isinstance(raw.get('Policy'),str):refuse()
    policy=json.loads(raw['Policy'],object_pairs_hook=unique)
    if (not isinstance(policy,dict) or set(policy)-{'Version','Id','Statement'}
            or policy.get('Version')!='2012-10-17'
            or ('Id' in policy and not isinstance(policy['Id'],str))):refuse()
    rules=policy.get('Statement')
    if not isinstance(rules,list) or len(rules)!=4:refuse()
    bucket='arn:aws:s3:::'+config.private_media_bucket
    object_resource=frozenset({bucket+'/media/v1/*'})
    bucket_resource=frozenset({bucket})
    all_resources=frozenset({bucket,bucket+'/*'})
    secure={'Bool':{'aws:securetransport':'true'}}
    insecure={'Bool':{'aws:securetransport':'false'}}
    found=set()
    for rule in rules:
        if (not isinstance(rule,dict) or set(rule)-{'Sid','Effect','Principal','Action','Resource','Condition'}
                or ('Sid' in rule and not isinstance(rule['Sid'],str))):refuse()
        effect=rule.get('Effect');principal=rule.get('Principal')
        actions=items(rule.get('Action'));resources=items(rule.get('Resource'))
        condition=conditions(rule.get('Condition'))
        if principal=='*':subjects=None
        elif isinstance(principal,dict) and set(principal)=={'CanonicalUser'}:subjects=items(principal['CanonicalUser'])
        else:refuse()
        kind=None
        if effect=='Allow' and condition==secure:
            if (subjects==frozenset({config.private_media_runtime_principal})
                    and actions==frozenset({'s3:GetObject','s3:PutObject'}) and resources==object_resource):kind='objects'
            elif (subjects==config.private_media_principals
                    and actions==frozenset({'s3:GetBucketAcl','s3:ListBucket'}) and resources==bucket_resource):kind='metadata'
        elif effect=='Deny' and actions in (frozenset({'*'}),frozenset({'s3:*'})) and resources==all_resources:
            if principal=='*' and condition==insecure:kind='transport'
            elif subjects==config.private_media_legacy_principals and condition=={}:kind='legacy'
        if kind is None or kind in found:refuse()
        found.add(kind)
    if found!={'objects','metadata','transport','legacy'}:refuse()


def check_bucket(config,client):
    validate_configuration(config)
    validate_acl(client.get_bucket_acl(Bucket=config.private_media_bucket),config.private_media_yandex_folder_id)
    validate_policy(client.get_bucket_policy(Bucket=config.private_media_bucket),config)
