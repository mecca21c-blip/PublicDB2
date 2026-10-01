"""Stable application-owned Korean administrative region choices."""

REGIONS = (
    ("SEOUL", "서울특별시"),
    ("BUSAN", "부산광역시"),
    ("DAEGU", "대구광역시"),
    ("INCHEON", "인천광역시"),
    ("GWANGJU", "광주광역시"),
    ("DAEJEON", "대전광역시"),
    ("ULSAN", "울산광역시"),
    ("SEJONG", "세종특별자치시"),
    ("GYEONGGI", "경기도"),
    ("GANGWON", "강원특별자치도"),
    ("CHUNGBUK", "충청북도"),
    ("CHUNGNAM", "충청남도"),
    ("JEONBUK", "전북특별자치도"),
    ("JEONNAM", "전라남도"),
    ("GYEONGBUK", "경상북도"),
    ("GYEONGNAM", "경상남도"),
    ("JEJU", "제주특별자치도"),
)
REGION_LABELS = dict(REGIONS)


def validate_region_code(value: str | None) -> str | None:
    cleaned = (value or "").strip().upper() or None
    if cleaned is not None and cleaned not in REGION_LABELS:
        raise ValueError("지원되는 지역을 선택하세요.")
    return cleaned
