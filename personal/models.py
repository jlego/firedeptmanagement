#coding=utf-8
from django.db import models
from common.models import Person
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django_auth_ldap.config import _LDAPConfig
from django_auth_ldap.backend import LDAPSettings
from utils.passwords import get_pronounceable_password, makeSecret
from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from sorl.thumbnail import ImageField
from django.db.models import Q, Sum
from local_settings import send_welcome_email
from local_settings import send_webmaster_email
from django.conf import settings
from datetime import date
from itertools import chain
import unidecode
begin_date = date(2000,12,31)

class Rank(models.Model):
    name = models.CharField(max_length=30)
    abrev = models.CharField(max_length=12)
    type = models.CharField(max_length=20)

    class Meta:
        verbose_name = "Rango"

    def __unicode__(self):
        return self.name


class Firefighter(Person):
    class Meta:
        verbose_name = "Bombero"
        ordering = ['-number', 'last_name']

    BLOOD_TYPE_CHOICES = (
        (u'O', u'O'),
        (u'A', u'A'),
        (u'B', u'B'),
        (u'AB', u'AB'),
    )

    BLOOD_RH_CHOICES = (
        (u'+', u'Positivo'),
        (u'-', u'Negativo'),
    )

    user = models.OneToOneField(User, null=True, blank=True)
    blood_type = models.CharField(max_length=2, choices=BLOOD_TYPE_CHOICES, null=True, blank=True, verbose_name=u'Factor Sanguineo')
    blood_type_rh = models.CharField(max_length=1, choices=BLOOD_RH_CHOICES, null=True, blank=True, verbose_name=u'RH')
    initials = models.CharField(max_length=4, null=True, blank=True, verbose_name=u'Iniciales')
    number = models.SmallIntegerField(null=True, blank=True, verbose_name=u'Carnet de Bombero')
    ranks = models.ManyToManyField(Rank, through="RankChange", null=True, verbose_name=u'Rangos')
    profile_picture = ImageField(upload_to="images/firefighter/", null=True, blank=True, verbose_name='Foto de Perfil', help_text=u"Asegurate de que el nombre del archivo no contenga acentos o eñes")
    
    @classmethod
    def search(cls, text):
        return cls.objects.filter(
            Q(initials__icontains=text) |
                           Q(initials=text) |
                           Q(first_name__istartswith=text) |
                           Q(first_name_2__istartswith=text) |
                           Q(last_name__istartswith=text) |
                           Q(last_name_2__istartswith=text) |
                           Q(id_document__istartswith=text) |
                           Q(number__istartswith=text) |
                           Q(primary_email__istartswith=text)
        ).order_by("last_name")

    def update_ldap_password(self, password = None):
        if not django_settings.AUTH_LDAP_BIND_PASSWORD:
            return
        
        ldap_c = _LDAPConfig.get_ldap()
        ldap_settings = LDAPSettings()
        conn = ldap_c.initialize(django_settings.AUTH_LDAP_SERVER_URI)
        conn.simple_bind_s(django_settings.AUTH_LDAP_BIND_DN, django_settings.AUTH_LDAP_BIND_PASSWORD)
        
        for opt, value in ldap_settings.CONNECTION_OPTIONS.iteritems():
            conn.set_option(opt, value)
        
        new_password = get_pronounceable_password() if not password else password
        username = self.primary_email.split("@")[0]
        mod_attrs = [(ldap_c.MOD_REPLACE, 'userPassword', makeSecret(new_password))]
        conn.modify_s('uid='+username+',ou=users,dc=bomberos,dc=usb,dc=ve', mod_attrs)

        send_welcome_email(str(self), username, new_password, self.alternate_email)
        
    def total_valid_arrests(self):
        minutes = self.arrests.filter(approved_by_ops=True, approved_by_inspector=True).aggregate(Sum('minutes'))['minutes__sum']
        return minutes if minutes else 0
    
    def total_valid_arrests_payments(self):
        minutes = self.arrests_payments.filter(approved_by_ops=True).aggregate(Sum('minutes'))['minutes__sum']
        return minutes if minutes else 0
    
    def total_arrests(self):
        return self.total_valid_arrests() - self.total_valid_arrests_payments()

    total_arrests.short_description = 'Minutos de Arresto'

    def current_rank(self):
        # Change the query for a related name 
        # search info for migrate and related name
        rank = ""
        ranks = RankChange.objects.filter(firefighter=self).order_by('-date')
        return ranks[0].rank_obtained.abrev if ranks else rank
    
    def arrests_and_payments(self):
        from ops.models import Arrest
        arrests = self.arrests.order_by('date')
        payments = self.arrests_payments.order_by('start_time')
        arrests_and_payments = list(chain(arrests,payments))
        arrests_and_payments = sorted(arrests_and_payments, key = lambda element: element.date if (element.__class__ == Arrest) else element.start_time.date())
        return arrests_and_payments[::-1]

    def previous_arrests_and_payments(self,year,month):
        minutes = self.arrests.filter(approved_by_ops=True, approved_by_inspector=True).filter(creation_date__range=[begin_date,date(year,month,1)]).aggregate(Sum('minutes'))['minutes__sum']
        arrests = minutes if minutes else 0
        minutes = self.arrests_payments.filter(approved_by_ops=True).filter(creation_date__range=[begin_date,date(year,month,1)]).aggregate(Sum('minutes'))['minutes__sum']
        payments = minutes if minutes else 0
        return arrests - payments

    def month_valid_arrests(self,year,month):
        minutes = self.arrests.filter(approved_by_ops=True, approved_by_inspector=True).filter(creation_date__year=int(year),creation_date__month=int(month)).aggregate(Sum('minutes'))['minutes__sum']
        return minutes if minutes else 0
    
    def month_valid_arrests_payments(self,year,month):
        minutes = self.arrests_payments.filter(approved_by_ops=True).filter(creation_date__year=int(year),creation_date__month=int(month)).aggregate(Sum('minutes'))['minutes__sum']
        return minutes if minutes else 0

    def current_condition_change(self):
        condition_changes =  self.condition_changes.all().select_related('condition').order_by("-date")
        return condition_changes[0] if condition_changes.count() else None
    
    def is_active(self):
        condition = self.current_condition_change() 
        return condition == None or (condition != None and condition.condition_id != settings.BAJ_CONDITION)
    
    def is_on_vacation(self):
        today = date.today()
        return True if self.holidays.filter(start_at__lt=today).filter(end_at__gt=today) else False
    
    def current_vacation(self):
        today = date.today()
        holidays = self.holidays.filter(start_at__lt=today).filter(end_at__gt=today)
        return holidays[0] if holidays.count() else None
    
class RankChange(models.Model):
    class Meta:
        verbose_name = u"Ascenso"
        ordering = ['date']

    firefighter = models.ForeignKey(Firefighter)
    rank_obtained = models.ForeignKey(Rank, verbose_name=u'Rango Obtenido')
    date = models.DateField(verbose_name=u'Fecha')
    link_to_doc = models.URLField(verbose_name=u'Link al comunicado', null=True, blank=True)

    def __unicode__(self):
        return str(self.rank_obtained) + " " + str(self.date)


class Condition(models.Model):
    class Meta:
        verbose_name = u"Condición"
        verbose_name_plural = u"Condiciones"

    name = models.CharField(max_length=30, verbose_name=u'Nombre')
    description = models.TextField(null=True, blank=True, verbose_name=u'Descripción')

    def __unicode__(self):
        return self.name


class ConditionChange(models.Model):
    class Meta:
        verbose_name = u"Cámbio de Condición"
        verbose_name_plural = u"Cambios de Condición"
        ordering = ["date"]

    firefighter = models.ForeignKey(Firefighter, verbose_name=u'Bombero',  related_name="condition_changes")
    condition = models.ForeignKey(Condition, verbose_name=u'Condición')
    date = models.DateField(verbose_name=u'Fecha')
    link_to_doc = models.URLField(verbose_name=u'Link al comunicado', null=True, blank=True)

    def __unicode__(self):
        return unicode(self.condition) + u" a " + unicode(self.firefighter) + u" el " + str(self.date) 


class Condecoration(models.Model):
    class Meta:
        verbose_name = u"Condecoración"
        verbose_name_plural = u"Condecoraciones"

    name = models.CharField(max_length=30, verbose_name=u'Nombre')
    description = models.TextField(null=True, blank=True, verbose_name=u'Descripción')

    def __unicode__(self):
        return unicode(self.name)


class CondecorationAward(models.Model):
    class Meta:
        verbose_name = u"Otorgamiento de Condecoración"
        verbose_name_plural = u"Otorgamientos de Condecoracion"
        ordering = ["date"]

    firefighter = models.ForeignKey(Firefighter, verbose_name=u'Bombero')
    condecoration = models.ForeignKey(Condecoration, verbose_name=u'Condecoración')
    date = models.DateField(verbose_name=u'Fecha')
    link_to_doc = models.URLField(verbose_name=u'Link al comunicado', null=True, blank=True)

    def __unicode__(self):
        return unicode(self.condecoration) + " el " + unicode(self.date) + " a " + unicode(self.firefighter)


class FirefighterHoliday(models.Model):
    firefighter = models.ForeignKey(Firefighter, verbose_name=u'Bombero',  related_name="holidays")
    start_at = models.DateField(u"Desde", db_index=True)
    end_at = models.DateField(u"Hasta", db_index=True)
    link_to_doc = models.URLField(verbose_name=u'Link al comunicado', null=True, blank=True)

    class Meta:
        verbose_name = u"Días de Permiso"
        verbose_name_plural = u"Días de Permiso"
        unique_together = ('firefighter', 'start_at', 'end_at')
        ordering = ["start_at"]
        
    def clean(self):
        if self.start_at > self.end_at:
            raise ValidationError(u'Desde debe ser una fecha menor que Hasta')
    
    def __unicode__(self):
        return unicode(self.firefighter) + " desde: " + unicode(self.start_at) + " hasta: " + unicode(self.end_at)
    

@receiver(post_save, sender=User)
def join_user_profile(sender, instance, created, **kwargs):
    if created:
        try:
            ff = Firefighter.objects.get(primary_email=instance.username + "@bomberos.usb.ve")
            ff.user = instance
            ff.save()
        except:
            pass

if django_settings.AUTH_LDAP_BIND_PASSWORD:
    @receiver(post_save, sender=Firefighter)
    def create_ldap_user(sender, instance, created, **kwargs):
        username = instance.first_name[0] + "".join(instance.last_name.split(" "))
        username = unidecode.unidecode(username.lower())
        
        if not created or User.objects.filter(username=username).count():
            return

        ldap_c = _LDAPConfig.get_ldap()

        ldap_settings = LDAPSettings()
        conn = ldap_c.initialize(django_settings.AUTH_LDAP_SERVER_URI)
        conn.simple_bind_s(django_settings.AUTH_LDAP_BIND_DN, django_settings.AUTH_LDAP_BIND_PASSWORD)
        for opt, value in ldap_settings.CONNECTION_OPTIONS.iteritems():
            conn.set_option(opt, value)

        uid = gid = 1500 + instance.id
        new_password = get_pronounceable_password()
        new_user_group = [
                    ('objectclass', ['posixGroup', 'top']),
                    ('gidNumber', str(gid)),
                    ]
        try:
            conn.add_s('cn=' + str(username) + ',ou=groups,dc=bomberos,dc=usb,dc=ve', new_user_group)
        except:
            pass

        new_user = [
                    ('objectclass', ['inetOrgPerson', 'posixAccount', 'top']),
                    ('gidNumber', str(gid)),
                    ('uidNumber', str(uid)),
                    ('sn', str(instance)),
                    ('givenName', str(instance.first_name.encode('UTF-8'))),
                    ('displayName', str(instance.first_name.encode('UTF-8')) + " " + str(instance.last_name.encode('UTF-8'))),
                    ('cn', str(instance.first_name.encode('UTF-8')) + " " + str(instance.last_name.encode('UTF-8'))),
                    ('homeDirectory', str('/home/') + str(username) + '/'),
                    ('loginShell', str('/bin/bash')),
                    ('userPassword', makeSecret(new_password)),
                    ('mail', username+"@bomberos.usb.ve"),
                    ]

        try:
            conn.add_s('uid=' + username + ',ou=users,dc=bomberos,dc=usb,dc=ve', new_user)
        except:
            pass
        mod_attrs = [(ldap_c.MOD_ADD, 'memberUid', username)]
        try:
            conn.modify_s('cn=cbvusb,ou=groups,dc=bomberos,dc=usb,dc=ve', mod_attrs)
        except:
            pass

        send_welcome_email(str(instance), username, new_password, instance.alternate_email)
        send_webmaster_email(username)
        instance.primary_email = username + "@bomberos.usb.ve"
        instance.save()

