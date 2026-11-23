from django import template

from ..time_format import format_time_12_hour

register = template.Library()


@register.filter
def time12(value: object) -> str:
    return format_time_12_hour(value)
