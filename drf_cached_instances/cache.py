"""BaseCache for foundation of app-specific caching strategy."""

from calendar import timegm
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pytz import utc
import json

from django.conf import settings
from django.db.models.loading import get_model
from rest_framework.serializers import BaseSerializer, PrimaryKeyRelatedField
from rest_framework.relations import HyperlinkedRelatedField

from .models import PkOnlyModel, PkOnlyQueryset


def extend(obj, attr, attr_value):
    """Returns a subclassed object with the added attribute."""
    obj_type = type(obj)
    falsey = False
    if obj is None:
        # NoneType singleton cannot be subclassed, create falsey object instead
        # TODO: create a singleton, save all empty attributes to the same object?
        obj_type = object
        falsey = True

    class extended(obj_type):
        """A class for adding attributes to objects if required by the cache."""

        def __init__(self, falsey):
            setattr(self, 'falsey', falsey)
            super().__init__

        def extend(self, attribute, value):
            setattr(self, attribute, value)
            return self

        def __bool__(self):
            return not self.falsey

    return extended(obj).extend(attr, attr_value)


class BaseCache(object):

    """Base instance cache.

    To make the cache useful, create a derived class with methods for
    your Django models.  See drf_cached_instances/tests/test_user_example.py
    for an example.
    """

    default_version = 'default'
    versions = ['default']

    def __init__(self):
        """Initialize BaseCache."""
        self._cache = None
        assert self.default_version in self.versions

    @property
    def cache(self):
        """Get the Django cache interface.

        This allows disabling the cache with
        settings.USE_DRF_INSTANCE_CACE=False.  It also delays import so that
        Django Debug Toolbar will record cache requests.
        """
        if not self._cache:
            use_cache = getattr(settings, 'USE_DRF_INSTANCE_CACHE', True)
            if use_cache:
                from django.core.cache import cache
                self._cache = cache
        return self._cache

    def key_for(self, version, model_name, obj_pk):
        """Get the cache key for the cached instance."""
        return 'drfc_{0}_{1}_{2}'.format(version, model_name, obj_pk)

    def delete_all_versions(self, model_name, obj_pk):
        """Delete all versions of a cached instance."""
        if self.cache:
            for version in self.versions:
                key = self.key_for(version, model_name, obj_pk)
                self.cache.delete(key)

    def model_function(self, model_name, version, func_name):
        """Return the model-specific caching function."""
        assert func_name in ('serializer', 'loader', 'invalidator', 'serializer_class')
        name = "%s_%s_%s" % (model_name.lower(), version, func_name)
        return getattr(self, name)

    def field_function(self, type_code, func_name):
        """Return the field function."""
        assert func_name in ('to_json', 'from_json')
        name = "field_%s_%s" % (type_code.lower(), func_name)
        return getattr(self, name)

    def field_to_json(self, type_code, key, *args, **kwargs):
        """Convert a field to a JSON-serializable representation."""
        assert ':' not in key
        to_json = self.field_function(type_code, 'to_json')
        key_and_type = "%s:%s" % (key, type_code)
        json_value = to_json(*args, **kwargs)
        return key_and_type, json_value

    def field_from_json(self, key_and_type, json_value):
        """Convert a JSON-serializable representation back to a field."""
        assert ':' in key_and_type
        key, type_code = key_and_type.split(':', 1)
        from_json = self.field_function(type_code, 'from_json')
        value = from_json(json_value)
        return key, value

    def value_with_attributes(self, name, value, field):
        """Modify value so that it can be serialized by field.
        """
        # print("Field " + str(field) + " has the type " + str(type(field)) + " requiring special treatment")
        if isinstance(field, BaseSerializer):
            # This must work also if the field is ListSerializer
            # If the value was produced by a serializer, we must traverse it
            if value is not None:
                return self.serialization_using_class(value, field)
            return value
        if isinstance(field, PrimaryKeyRelatedField):
            # Primary key must be accessible by pk attribute
            # print(str(name) + ".pk now also has the value " + str(value))
            return extend(value, 'pk', value)
        return value

    def serialization_using_class(self, serialization, serializer):
        """Recreates the serialization OrderedDict with sources defined
        as required by the accompanied serializer, essentially mocking the db.
        """
        # OrderedDict keys are immutable, so we have to reconstruct the serialization
        # print("The serializer is " + str(serializer))
        # print("The serialization is " + str(serialization))

        ret = []

        if not isinstance(serialization, list):
            # always encapsulate the serializer in a list
            serialization_list = [serialization]
        else:
            # if the serialization is a list, find the child serializer
            serialization_list = serialization
            serializer = serializer.child

        for ser in serialization_list:
            # The new serialization will be in the same order
            new_ser = OrderedDict()

            for name, value in list(ser.items()):
                # print(str(name) + " had the value " + str(value))

                # Check if the value requires added attributes
                try:
                    field = serializer.fields[name]
                    value = self.value_with_attributes(name, value, field)
                    source = field.source
                    # print("Field is " + str(field) + ", value is " + str(value) + " and source is " + str(source))
                except KeyError as key_error:
                    # If the serialization was produced by rest_framework_gis, value may be absent
                    try:
                        # print("geo_fields is " + str(serializer.geo_fields))
                        if serializer.geo_fields:
                            # Leave GeoModelSerializer generated fields untouched
                            field = None
                            value = value
                            source = name
                    except AttributeError:
                        raise key_error
                # Replace the names with the sources
                if source is not '*':  # asterisk stands for reference to the model object itself
                    # Sources with . must remain readable as is, but also contain the attribute
                    source, delimiter, attribute = source.partition('.')
                    # This is the magic line
                    name = source
                    if attribute:
                        # We have to piggyback the object with repeated data
                        value = extend(value, attribute, value)
                        # print(str(name) + '.' + str(attribute) + " now also has the value " + str(value))
                if isinstance(field, HyperlinkedRelatedField):
                    # url fields require their lookup_field to be added as object attribute
                    if source is '*':
                        # the field looks at the parent serializer for the object pk
                        setattr(new_ser, field.lookup_field, ser['id'])
                    else:
                        # the field looks at the db for the external object pk, we parse it from url
                        # print("The url is " + value + " and the pk is " + value.rsplit('/')[-2])
                        value = extend(value, field.lookup_field, value.rsplit('/')[-2])

                # print(str(name) + " now has the value " + str(value))
                new_ser[name] = value
            ret.append(new_ser)

        # If the serialization was not a list, return a bare serialization
        if not isinstance(serialization, list):
            return ret[0]
        else:
            return ret

    def get_instances(self, object_specs, version=None):
        """Get the cached native representation for one or more objects.

        Keyword arguments:
        object_specs - A sequence of triples (model name, pk, obj):
        - model name - the name of the model
        - pk - the primary key of the instance
        - obj - the instance, or None to load it
        version - The cache version to use, or None for default

        To get the 'new object' representation, set pk and obj to None

        Return is a dictionary:
        key - (model name, pk)
        value - (native representation, pk, object or None)
        """
        ret = dict()
        spec_keys = set()
        cache_keys = []
        version = version or self.default_version

        # Construct all the cache keys to fetch
        for model_name, obj_pk, obj in object_specs:
            assert model_name
            assert obj_pk

            # Get cache keys to fetch
            obj_key = self.key_for(version, model_name, obj_pk)
            spec_keys.add((model_name, obj_pk, obj, obj_key))
            cache_keys.append(obj_key)

        # Fetch the cache keys
        if cache_keys and self.cache:
            cache_vals = self.cache.get_many(cache_keys)
        else:
            cache_vals = {}

        # Use cached representations, or recreate
        cache_to_set = {}
        for model_name, obj_pk, obj, obj_key in spec_keys:

            # Get the right serializer
            try:
                serializer_class = self.model_function(
                    model_name, version, 'serializer_class')()
            except AttributeError:
                serializer_class = None
            try:
                serializer = self.model_function(
                    model_name, version, 'serializer')
            except AttributeError:
                serializer = None
            if serializer_class:
                # If the class is provided, it overrides the native serializer:
                serializer = serializer_class.to_representation
            assert serializer

            # Load cached objects
            obj_val = cache_vals.get(obj_key)
            obj_native = json.loads(obj_val) if obj_val else None

            # Invalid or not set - load from database
            if not obj_native:
                if not obj:
                    loader = self.model_function(model_name, version, 'loader')
                    obj = loader(obj_pk)
                obj_native = serializer(obj) or {}
                if obj_native:
                    cache_to_set[obj_key] = json.dumps(obj_native)

            # Get fields to convert
            keys = [key for key in obj_native.keys() if ':' in key]
            for key in keys:
                json_value = obj_native.pop(key)
                name, value = self.field_from_json(key, json_value)
                assert name not in obj_native
                obj_native[name] = value

            # Native object found
            if obj_native:
                if serializer_class:
                    # Mock the db for the serialization class
                    obj_native = self.serialization_using_class(obj_native, serializer_class)
                # Reconstructing the object from the serialization
                ret[(model_name, obj_pk)] = (obj_native, obj_key, obj)
                # print("Now the serialization is " + str(ret))

        # Save any new cached representations
        if cache_to_set and self.cache:
            self.cache.set_many(cache_to_set)

        return ret

    def update_instance(
            self, model_name, pk, instance=None, version=None,
            update_only=False):
        """Create or update a cached instance.

        Keyword arguments are:
        model_name - The name of the model
        pk - The primary key of the instance
        instance - The Django model instance, or None to load it
        versions - Version to update, or None for all
        update_only - If False (default), then missing cache entries will be
            populated and will cause follow-on invalidation.  If True, then
            only entries already in the cache will be updated and cause
            follow-on invalidation.

        Return is a list of tuples (model name, pk, immediate) that also needs
        to be updated.
        """
        versions = [version] if version else self.versions
        invalid = []
        for version in versions:
            try:
                serializer_class = self.model_function(model_name, version, 'serializer_class')()
            except AttributeError:
                serializer_class = None
            try:
                serializer = self.model_function(model_name, version, 'serializer')
            except AttributeError:
                serializer = None
            if serializer_class:
                # If the class is provided, it overrides the native serializer:
                serializer = serializer_class.to_representation
            loader = self.model_function(model_name, version, 'loader')
            invalidator = self.model_function(
                model_name, version, 'invalidator')
            if serializer is None and serializer_class is None and loader is None and invalidator is None:
                continue

            if self.cache is None:
                continue

            # Try to load the instance
            if not instance:
                instance = loader(pk)

            if serializer:
                # Get current value, if in cache
                key = self.key_for(version, model_name, pk)
                current_raw = self.cache.get(key)
                current = json.loads(current_raw) if current_raw else None

                # Get new value
                if update_only and current_raw is None:
                    new = None
                else:
                    new = serializer(instance)
                deleted = not instance

                # If cache is invalid, update cache
                invalidate = (current != new) or deleted
                if invalidate:
                    if deleted:
                        self.cache.delete(key)
                    else:
                        self.cache.set(key, json.dumps(new))
            else:
                invalidate = True

            # Invalidate upstream caches
            if instance and invalidate:
                for upstream in invalidator(instance):
                    if isinstance(upstream, str):
                        self.cache.delete(upstream)
                    else:
                        m, i, immediate = upstream
                        if immediate:
                            invalidate_key = self.key_for(version, m, i)
                            self.cache.delete(invalidate_key)
                        invalid.append((m, i, version))
        return invalid

    #
    # Built-in Field converters
    #

    def field_date_from_json(self, date_triple):
        """Convert a date triple to the date."""
        return date(*date_triple) if date_triple else None

    def field_date_to_json(self, day):
        """Convert a date to a date triple."""
        return [day.year, day.month, day.day] if day else None

    def field_datetime_from_json(self, json_val):
        """Convert a UTC timestamp to a UTC datetime."""
        if type(json_val) == int:
            seconds = int(json_val)
            dt = datetime.fromtimestamp(seconds, utc)
        else:
            seconds, microseconds = [int(x) for x in json_val.split('.')]
            dt = datetime.fromtimestamp(seconds, utc)
            dt += timedelta(microseconds=microseconds)
        return dt

    def field_datetime_to_json(self, dt):
        """Convert a datetime to a UTC timestamp w/ microsecond resolution.

        datetimes w/o timezone will be assumed to be in UTC
        """
        ts = timegm(dt.utctimetuple())
        if dt.microsecond:
            return "{0}.{1:0>6d}".format(ts, dt.microsecond)
        else:
            return ts

    def field_pklist_from_json(self, data):
        """Load a PkOnlyQueryset from a JSON dict.

        This uses the same format as cached_queryset_from_json
        """
        model = get_model(data['app'], data['model'])
        return PkOnlyQueryset(self, model, data['pks'])

    def field_pklist_to_json(self, model, pks):
        """Convert a list of primary keys to a JSON dict.

        This uses the same format as cached_queryset_to_json
        """
        app_label = model._meta.app_label
        model_name = model._meta.model_name
        return {
            'app': app_label,
            'model': model_name,
            'pks': list(pks),
        }

    def field_pk_from_json(self, data):
        """Load a PkOnlyModel from a JSON dict."""
        model = get_model(data['app'], data['model'])
        return PkOnlyModel(self, model, data['pk'])

    def field_pk_to_json(self, model, pk):
        """Convert a primary key to a JSON dict."""
        app_label = model._meta.app_label
        model_name = model._meta.model_name
        return {
            'app': app_label,
            'model': model_name,
            'pk': pk,
        }
