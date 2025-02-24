from django_auth_ldap.backend import LDAPBackend, _LDAPUser
from django.conf import settings
from sentry.models import (
    Organization,
    OrganizationMember,
    UserEmail,
    UserOption,
)
import re
import logging

logger = logging.getLogger('django_auth_ldap')


def _get_effective_sentry_role(ldap_user):
    role_priority_order = [
        'member',
        'admin',
        'manager',
        'owner',
    ]

    role_mapping = getattr(settings, 'AUTH_LDAP_SENTRY_GROUP_ROLE_MAPPING', None)
    if not role_mapping:
        return None

    group_names = ldap_user.group_names
    if not group_names:
        return None

    applicable_roles = [role for role, groups in role_mapping.items() if group_names.intersection(groups)]
    if not applicable_roles:
        return None

    highest_role = [role for role in role_priority_order if role in applicable_roles][-1]
    return highest_role


class SentryLdapBackend(LDAPBackend):
    # # Override ldap_to_django_username to preprocess the LDAP user
    # def ldap_to_django_username(self, username):
    #     # Remove the domain part from the username
    #     logger.info(f'Preprocessed LDAP username: {username}')
    #
    #     email_pattern = re.compile(r'^(.+)@')
    #     match = email_pattern.match(username)
    #     if match:
    #         username = match.group(1)
    #
    #     logger.info(f'Preprocessed LDAP after username: {username}')
    #     return super().ldap_to_django_username(username)

    # def authenticate(self, request=None, username=None, password=None, **kwargs):
    #     logger.info(f'Custom authenticate LDAP username: {username}')
    #
    #     # if username.find("@") == -1:
    #     #     logger.debug('Rejecting not mail for %s' % username)
    #     #     return None
    #
    #     if bool(password) or self.settings.PERMIT_EMPTY_PASSWORD:
    #         ldap_user = _LDAPUser(self, username=username.split("@")[0].strip())
    #         user = ldap_user.authenticate(password)
    #     else:
    #         logger.debug('Rejecting empty password for %s' % username)
    #         user = None
    #
    #     return user

    def authenticate(
        self, request=None, username=None, password=None, **kwargs
    ):
        logger.info(f'Custom authenticate LDAP username: {username}')

        # if (
        #     username.find('@') == -1
        #     or username.split('@')[1] != settings.AUTH_LDAP_DEFAULT_EMAIL_DOMAIN
        # ):
        #     return None
        ldap_user = _LDAPUser(self, username=username.split('@')[0].strip())
        user = ldap_user.authenticate(password)
        return user

    # def ldap_to_django_username(self, username):
    #     logger.info(f'ldap_to_django_username LDAP username: {username}')
    #
    #     """Override LDAPBackend function to get the username with domain"""
    #     return username + '@' + settings.AUTH_LDAP_DEFAULT_EMAIL_DOMAIN

    def django_to_ldap_username(self, username):
        logger.info(f'django_to_ldap_username LDAP username: {username}')

        """Override LDAPBackend function to get the real LDAP username"""
        return username.split('@')[0]

    def get_or_build_user(self, username, ldap_user):
        (user, built) = super().get_or_build_user(username, ldap_user)

        user.is_managed = True

        logger.info(f'get_or_build_user LDAP username: {username}')

        # Add the user email address
        mail_attr_name = self.settings.USER_ATTR_MAP.get('email', 'mail')
        mail_attr = ldap_user.attrs.get(mail_attr_name)
        if mail_attr:
            email = mail_attr[0]
        elif hasattr(settings, 'AUTH_LDAP_DEFAULT_EMAIL_DOMAIN'):
            # email = username + '@' + settings.AUTH_LDAP_DEFAULT_EMAIL_DOMAIN
            email = username
        else:
            email = None

        if email:
            user.email = email

        user.save()

        if mail_attr and getattr(settings, 'AUTH_LDAP_MAIL_VERIFIED', False):
            defaults = {'is_verified': True}
        else:
            defaults = None

        for mail in mail_attr or [email]:
            UserEmail.objects.update_or_create(defaults=defaults, user=user, email=mail)

        # Check to see if we need to add the user to an organization
        organization_slug = getattr(settings, 'AUTH_LDAP_SENTRY_DEFAULT_ORGANIZATION', None)
        # For backward compatibility
        organization_name = getattr(settings, 'AUTH_LDAP_DEFAULT_SENTRY_ORGANIZATION', None)

        # Find the default organization
        if organization_slug:
            organizations = Organization.objects.filter(slug=organization_slug)
        elif organization_name:
            organizations = Organization.objects.filter(name=organization_name)
        else:
            return (user, built)

        if not organizations or len(organizations) < 1:
            return (user, built)

        member_role = _get_effective_sentry_role(ldap_user) or getattr(settings,
                                                                       'AUTH_LDAP_SENTRY_ORGANIZATION_ROLE_TYPE', None)

        has_global_access = getattr(settings, 'AUTH_LDAP_SENTRY_ORGANIZATION_GLOBAL_ACCESS', False)

        # Add the user to the organization with global access
        OrganizationMember.objects.update_or_create(
            organization=organizations[0],
            user_id=user.id,
            defaults={
                'role': member_role,
                'has_global_access': has_global_access,
                'flags': getattr(OrganizationMember.flags, 'sso:linked')
            }
        )

        if not getattr(settings, 'AUTH_LDAP_SENTRY_SUBSCRIBE_BY_DEFAULT', True):
            UserOption.objects.set_value(
                user=user,
                project=None,
                key='subscribe_by_default',
                value='0',
            )

        return (user, built)
