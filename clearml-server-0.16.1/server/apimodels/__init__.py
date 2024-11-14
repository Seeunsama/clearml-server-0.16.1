from __future__ import absolute_import

from enum import Enum
from typing import Union, Type, Iterable

import jsonmodels.errors
import six
from jsonmodels import fields
from jsonmodels.fields import _LazyType, NotSet
from jsonmodels.models import Base as ModelBase
from jsonmodels.validators import Enum as EnumValidator
from luqum.parser import parser, ParseError
from validators import email as email_validator, domain as domain_validator

from apierrors import errors
from utilities.json import loads, dumps


def make_default(field_cls, default_value):
    class _FieldWithDefault(field_cls):
        def get_default_value(self):
            return default_value

    return _FieldWithDefault


class ListField(fields.ListField):
    def __init__(self, items_types=None, *args, default=NotSet, **kwargs):
        if default is not NotSet and callable(default):
            default = default()

        super(ListField, self).__init__(items_types, *args, default=default, **kwargs)

    def _cast_value(self, value):
        try:
            return super(ListField, self)._cast_value(value)
        except TypeError:
            return value

    def validate_single_value(self, item):
        super(ListField, self).validate_single_value(item)
        if isinstance(item, ModelBase):
            item.validate()


class DictField(fields.BaseField):
    types = (dict,)

    def __init__(self, value_types=None, *args, **kwargs):
        self.value_types = self._assign_types(value_types)
        super(DictField, self).__init__(*args, **kwargs)

    def get_default_value(self):
        default = super(DictField, self).get_default_value()
        if default is None and not self.required:
            return {}
        return default

    @staticmethod
    def _assign_types(value_types):
        if value_types:
            try:
                value_types = tuple(value_types)
            except TypeError:
                value_types = (value_types,)
        else:
            value_types = tuple()

        return tuple(
            _LazyType(type_) if isinstance(type_, six.string_types) else type_
            for type_ in value_types
        )

    def validate(self, value):
        super(DictField, self).validate(value)

        if not self.value_types:
            return

        if not value:
            return

        for item in value.values():
            self.validate_single_value(item)

    def validate_single_value(self, item):
        if not self.value_types:
            return

        if not isinstance(item, self.value_types):
            raise jsonmodels.errors.ValidationError(
                "All items must be instances "
                'of "{types}", and not "{type}".'.format(
                    types=", ".join([t.__name__ for t in self.value_types]),
                    type=type(item).__name__,
                )
            )


class IntField(fields.IntField):
    def parse_value(self, value):
        try:
            return super(IntField, self).parse_value(value)
        except (ValueError, TypeError):
            return value


def validate_lucene_query(value):
    if value == "":
        return
    try:
        parser.parse(value)
    except ParseError as e:
        raise errors.bad_request.InvalidLuceneSyntax(error=e)


class LuceneQueryField(fields.StringField):
    def validate(self, value):
        super(LuceneQueryField, self).validate(value)
        if value is None:
            return
        validate_lucene_query(value)


class NullableEnumValidator(EnumValidator):
    """Validator for enums that allows a None value."""

    def validate(self, value):
        if value is not None:
            super(NullableEnumValidator, self).validate(value)


class EnumField(fields.StringField):
    def __init__(
        self,
        values_or_type: Union[Iterable, Type[Enum]],
        *args,
        required=False,
        default=None,
        **kwargs
    ):
        choices = list(map(self.parse_value, values_or_type))
        validator_cls = EnumValidator if required else NullableEnumValidator
        kwargs.setdefault("validators", []).append(validator_cls(*choices))
        super().__init__(
            default=self.parse_value(default), required=required, *args, **kwargs
        )

    def parse_value(self, value):
        if isinstance(value, Enum):
            return str(value.value)
        return super().parse_value(value)


class ActualEnumField(fields.StringField):
    def __init__(
        self,
        enum_class: Type[Enum],
        *args,
        validators=None,
        required=False,
        default=None,
        **kwargs
    ):
        self.__enum = enum_class
        self.types = (enum_class,)
        # noinspection PyTypeChecker
        choices = list(enum_class)
        validator_cls = EnumValidator if required else NullableEnumValidator
        validators = [*(validators or []), validator_cls(*choices)]
        super().__init__(
            default=self.parse_value(default) if default else NotSet,
            *args,
            required=required,
            validators=validators,
            **kwargs
        )

    def parse_value(self, value):
        if value is None and not self.required:
            return self.get_default_value()
        try:
            # noinspection PyArgumentList
            return self.__enum(value)
        except ValueError:
            return value

    def to_struct(self, value):
        return super().to_struct(value.value)


class EmailField(fields.StringField):
    def validate(self, value):
        super().validate(value)
        if value is None:
            return
        if email_validator(value) is not True:
            raise errors.bad_request.InvalidEmailAddress()


class DomainField(fields.StringField):
    def validate(self, value):
        super().validate(value)
        if value is None:
            return
        if domain_validator(value) is not True:
            raise errors.bad_request.InvalidDomainName()


class JsonSerializableMixin:
    def to_json(self: ModelBase):
        return dumps(self.to_struct())

    @classmethod
    def from_json(cls: Type[ModelBase], s):
        return cls(**loads(s))
