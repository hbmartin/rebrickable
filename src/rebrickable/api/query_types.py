"""Generated query keyword contracts for the Rebrickable API."""

from typing import TypedDict

OPENAPI_SHA256 = "3dc9f0e24ecec0e16c02358a91af51766cae4a1181ed0fe5197d0fa9fd100cea"


class LegoColorsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    ordering: str


class LegoColorsReadQuery(TypedDict, total=False):
    ordering: str


class LegoMinifigsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    min_parts: int | float
    max_parts: int | float
    in_set_num: str
    in_theme_id: str
    ordering: str
    search: str


class LegoMinifigsPartsListQuery(TypedDict, total=False):
    page: int
    page_size: int


class LegoMinifigsSetsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    ordering: str


class LegoPartCategoriesListQuery(TypedDict, total=False):
    page: int
    page_size: int
    ordering: str


class LegoPartCategoriesReadQuery(TypedDict, total=False):
    ordering: str


class LegoPartsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    part_num: str
    part_nums: str
    part_cat_id: str
    color_id: str
    bricklink_id: str
    brickowl_id: str
    lego_id: str
    ldraw_id: str
    ordering: str
    search: str
    inc_part_details: bool


class LegoPartsReadQuery(TypedDict, total=False):
    inc_part_details: bool


class LegoPartsColorsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    ordering: str


class LegoPartsColorsSetsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    ordering: str


class LegoSetsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    theme_id: str
    min_year: int | float
    max_year: int | float
    min_parts: int | float
    max_parts: int | float
    ordering: str
    search: str


class LegoSetsAlternatesListQuery(TypedDict, total=False):
    page: int
    page_size: int
    ordering: str


class LegoSetsMinifigsListQuery(TypedDict, total=False):
    page: int
    page_size: int


class LegoSetsPartsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    inc_part_details: bool
    inc_color_details: bool
    inc_minifig_parts: bool


class LegoSetsSetsListQuery(TypedDict, total=False):
    page: int
    page_size: int


class LegoThemesListQuery(TypedDict, total=False):
    page: int
    page_size: int
    ordering: str


class LegoThemesReadQuery(TypedDict, total=False):
    ordering: str


class UsersBadgesListQuery(TypedDict, total=False):
    page: int
    page_size: int
    ordering: str


class UsersBadgesReadQuery(TypedDict, total=False):
    ordering: str


class UsersAllpartsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    part_num: str
    part_cat_id: int | float
    color_id: int | float
    inc_part_details: bool


class UsersLostPartsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    ordering: str


class UsersLostPartsDeleteQuery(TypedDict, total=False):
    ordering: str


class UsersMinifigsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    fig_set_num: str
    ordering: str
    search: str


class UsersPartlistsListQuery(TypedDict, total=False):
    page: int
    page_size: int


class UsersPartlistsPartsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    ordering: str
    inc_part_details: bool
    inc_color_details: bool


class UsersPartlistsPartsReadQuery(TypedDict, total=False):
    ordering: str


class UsersPartlistsPartsUpdateQuery(TypedDict, total=False):
    ordering: str


class UsersPartlistsPartsDeleteQuery(TypedDict, total=False):
    ordering: str


class UsersPartsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    part_num: str
    part_cat_id: int | float
    color_id: int | float
    ordering: str
    search: str
    inc_part_details: bool


class UsersSetlistsListQuery(TypedDict, total=False):
    page: int
    page_size: int


class UsersSetlistsSetsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    ordering: str


class UsersSetlistsSetsReadQuery(TypedDict, total=False):
    ordering: str


class UsersSetlistsSetsUpdateQuery(TypedDict, total=False):
    ordering: str


class UsersSetlistsSetsPartialUpdateQuery(TypedDict, total=False):
    ordering: str


class UsersSetlistsSetsDeleteQuery(TypedDict, total=False):
    ordering: str


class UsersSetsListQuery(TypedDict, total=False):
    page: int
    page_size: int
    set_num: str
    theme_id: int | float
    min_year: int | float
    max_year: int | float
    min_parts: int | float
    max_parts: int | float
    ordering: str
    search: str


class UsersSetsReadQuery(TypedDict, total=False):
    theme_id: int | float
    min_year: int | float
    max_year: int | float
    min_parts: int | float
    max_parts: int | float
    ordering: str
    search: str


class UsersSetsUpdateQuery(TypedDict, total=False):
    theme_id: int | float
    min_year: int | float
    max_year: int | float
    min_parts: int | float
    max_parts: int | float
    ordering: str
    search: str


class UsersSetsDeleteQuery(TypedDict, total=False):
    theme_id: int | float
    min_year: int | float
    max_year: int | float
    min_parts: int | float
    max_parts: int | float
    ordering: str
    search: str
