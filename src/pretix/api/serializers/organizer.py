from decimal import Decimal

from django.db.models import Q
from django.utils.translation import get_language, gettext_lazy as _
from hierarkey.proxy import HierarkeyProxy
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from pretix.api.serializers.i18n import I18nAwareModelSerializer
from pretix.api.serializers.order import CompatibleJSONField
from pretix.base.auth import get_auth_backends
from pretix.base.models import (
    Device, GiftCard, GiftCardTransaction, Organizer, SeatingPlan, Team,
    TeamAPIToken, TeamInvite, User,
)
from pretix.base.models.seating import SeatingPlanLayoutValidator
from pretix.base.services.mail import SendMailException, mail
from pretix.base.settings import DEFAULTS, validate_organizer_settings
from pretix.helpers.urls import build_absolute_uri


class OrganizerSerializer(I18nAwareModelSerializer):
    class Meta:
        model = Organizer
        fields = ('name', 'slug')


class SeatingPlanSerializer(I18nAwareModelSerializer):
    layout = CompatibleJSONField(
        validators=[SeatingPlanLayoutValidator()]
    )

    class Meta:
        model = SeatingPlan
        fields = ('id', 'name', 'layout')


class GiftCardSerializer(I18nAwareModelSerializer):
    value = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.00'))

    def validate(self, data):
        data = super().validate(data)
        s = data['secret']
        qs = GiftCard.objects.filter(
            secret=s
        ).filter(
            Q(issuer=self.context["organizer"]) | Q(
                issuer__gift_card_collector_acceptance__collector=self.context["organizer"])
        )
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                {'secret': _(
                    'A gift card with the same secret already exists in your or an affiliated organizer account.')}
            )
        return data

    class Meta:
        model = GiftCard
        fields = ('id', 'secret', 'issuance', 'value', 'currency', 'testmode', 'expires', 'conditions')


class OrderEventSlugField(serializers.RelatedField):

    def to_representation(self, obj):
        return obj.event.slug


class GiftCardTransactionSerializer(I18nAwareModelSerializer):
    order = serializers.SlugRelatedField(slug_field='code', read_only=True)
    event = OrderEventSlugField(source='order', read_only=True)

    class Meta:
        model = GiftCardTransaction
        fields = ('id', 'datetime', 'value', 'event', 'order', 'text')


class EventSlugField(serializers.SlugRelatedField):
    def get_queryset(self):
        return self.context['organizer'].events.all()


class TeamSerializer(serializers.ModelSerializer):
    limit_events = EventSlugField(slug_field='slug', many=True)

    class Meta:
        model = Team
        fields = (
            'id', 'name', 'all_events', 'limit_events', 'can_create_events', 'can_change_teams',
            'can_change_organizer_settings', 'can_manage_gift_cards', 'can_change_event_settings',
            'can_change_items', 'can_view_orders', 'can_change_orders', 'can_view_vouchers',
            'can_change_vouchers'
        )

    def validate(self, data):
        full_data = self.to_internal_value(self.to_representation(self.instance)) if self.instance else {}
        full_data.update(data)
        if full_data.get('limit_events') and full_data.get('all_events'):
            raise ValidationError('Do not set both limit_events and all_events.')
        return data


class DeviceSerializer(serializers.ModelSerializer):
    limit_events = EventSlugField(slug_field='slug', many=True)
    device_id = serializers.IntegerField(read_only=True)
    unique_serial = serializers.CharField(read_only=True)
    hardware_brand = serializers.CharField(read_only=True)
    hardware_model = serializers.CharField(read_only=True)
    software_brand = serializers.CharField(read_only=True)
    software_version = serializers.CharField(read_only=True)
    created = serializers.DateTimeField(read_only=True)
    revoked = serializers.BooleanField(read_only=True)
    initialized = serializers.DateTimeField(read_only=True)
    initialization_token = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Device
        fields = (
            'device_id', 'unique_serial', 'initialization_token', 'all_events', 'limit_events',
            'revoked', 'name', 'created', 'initialized', 'hardware_brand', 'hardware_model',
            'software_brand', 'software_version', 'security_profile'
        )


class TeamInviteSerializer(serializers.ModelSerializer):
    class Meta:
        model = TeamInvite
        fields = (
            'id', 'email'
        )

    def _send_invite(self, instance):
        try:
            mail(
                instance.email,
                _('pretix account invitation'),
                'pretixcontrol/email/invitation.txt',
                {
                    'user': self,
                    'organizer': self.context['organizer'].name,
                    'team': instance.team.name,
                    'url': build_absolute_uri('control:auth.invite', kwargs={
                        'token': instance.token
                    })
                },
                event=None,
                locale=get_language()  # TODO: expose?
            )
        except SendMailException:
            pass  # Already logged

    def create(self, validated_data):
        if 'email' in validated_data:
            try:
                user = User.objects.get(email__iexact=validated_data['email'])
            except User.DoesNotExist:
                if self.context['team'].invites.filter(email__iexact=validated_data['email']).exists():
                    raise ValidationError(_('This user already has been invited for this team.'))
                if 'native' not in get_auth_backends():
                    raise ValidationError('Users need to have a pretix account before they can be invited.')

                invite = self.context['team'].invites.create(email=validated_data['email'])
                self._send_invite(invite)
                invite.team.log_action(
                    'pretix.team.invite.created',
                    data={
                        'email': validated_data['email']
                    },
                    **self.context['log_kwargs']
                )
                return invite
            else:
                if self.context['team'].members.filter(pk=user.pk).exists():
                    raise ValidationError(_('This user already has permissions for this team.'))

                self.context['team'].members.add(user)
                self.context['team'].log_action(
                    'pretix.team.member.added',
                    data={
                        'email': user.email,
                        'user': user.pk,
                    },
                    **self.context['log_kwargs']
                )
                return TeamInvite(email=user.email)
        else:
            raise ValidationError('No email address given.')


class TeamAPITokenSerializer(serializers.ModelSerializer):
    active = serializers.BooleanField(default=True, read_only=True)

    class Meta:
        model = TeamAPIToken
        fields = (
            'id', 'name', 'active'
        )


class TeamMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            'id', 'email', 'fullname', 'require_2fa'
        )


class OrganizerSettingsSerializer(serializers.Serializer):
    default_fields = [
        'organizer_info_text',
        'event_list_type',
        'event_list_availability',
        'organizer_homepage_text',
        'organizer_link_back',
        'organizer_logo_image_large',
        'giftcard_length',
        'giftcard_expiry_years',
        'locales',
        'event_team_provisioning',
        'primary_color',
        'theme_color_success',
        'theme_color_danger',
        'theme_color_background',
        'theme_round_borders',
        'primary_font'
    ]

    def __init__(self, *args, **kwargs):
        self.organizer = kwargs.pop('organizer')
        self.changed_data = []
        super().__init__(*args, **kwargs)
        for fname in self.default_fields:
            kwargs = DEFAULTS[fname].get('serializer_kwargs', {})
            if callable(kwargs):
                kwargs = kwargs()
            kwargs.setdefault('required', False)
            kwargs.setdefault('allow_null', True)
            form_kwargs = DEFAULTS[fname].get('form_kwargs', {})
            if callable(form_kwargs):
                form_kwargs = form_kwargs()
            if 'serializer_class' not in DEFAULTS[fname]:
                raise ValidationError('{} has no serializer class'.format(fname))
            f = DEFAULTS[fname]['serializer_class'](
                **kwargs
            )
            f._label = form_kwargs.get('label', fname)
            f._help_text = form_kwargs.get('help_text')
            self.fields[fname] = f

    def update(self, instance: HierarkeyProxy, validated_data):
        for attr, value in validated_data.items():
            if value is None:
                instance.delete(attr)
                self.changed_data.append(attr)
            elif instance.get(attr, as_type=type(value)) != value:
                instance.set(attr, value)
                self.changed_data.append(attr)
        return instance

    def validate(self, data):
        data = super().validate(data)
        settings_dict = self.instance.freeze()
        settings_dict.update(data)
        validate_organizer_settings(self.organizer, settings_dict)
        return data
