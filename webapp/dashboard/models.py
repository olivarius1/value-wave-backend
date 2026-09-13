"""Web 控制台自有数据：后台任务账本 / 股票分组标签。

分数、K线、财务等主数据与派生数据都不在此存：
- K线/财务/因子/模型分类 → 既有 SQLite 库（kline_store.db / fin_store.db，经 scripts/ 模块读写）
- 分数与历史分位 → 评分器产出（artifacts/.cache/score_store.db，score_market.py 写、Web 只读）
- 报告 HTML / 估值 JSON → artifacts/reports 与 artifacts/json_data（build_report.py 写）
"""
from django.db import models


class Job(models.Model):
    """后台任务账本。长任务（全市场抓取/分类/评分/报告）串行执行，页面轮询进度。"""

    STATUS = (
        ('pending', '排队中'),
        ('running', '运行中'),
        ('done', '完成'),
        ('failed', '失败'),
        ('canceled', '已取消'),
    )

    kind = models.CharField(max_length=40, db_index=True)
    label = models.CharField(max_length=120, blank=True, default='')
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=STATUS, default='pending', db_index=True)
    progress = models.IntegerField(default=0)
    total = models.IntegerField(default=0)
    message = models.CharField(max_length=300, blank=True, default='')
    log = models.TextField(blank=True, default='')
    pid = models.IntegerField(null=True, blank=True)
    returncode = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f'#{self.id} {self.kind} {self.status}'



class StockGroup(models.Model):
    """股票分组（标签）。内置「自选」组从 watchlist.txt 播种；一只股票可属于多个组。"""
    name = models.CharField(max_length=60, unique=True)
    is_builtin = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.name}({self.member_count})'

    @property
    def member_count(self):
        return self.members.count()

    @classmethod
    def ensure_builtin(cls):
        """内置「自选」组：不存在时创建并从 watchlist.txt 播种成员"""
        builtin = cls.objects.filter(is_builtin=True).first()
        if builtin:
            return builtin
        from .services.stock_ops import load_watchlist
        builtin = cls.objects.create(name='自选', is_builtin=True)
        codes = [c for c in load_watchlist() if c.isdigit() and len(c) == 6]
        StockGroupMember.objects.bulk_create(
            [StockGroupMember(group=builtin, code=c) for c in codes],
            ignore_conflicts=True)
        return builtin

    def codes(self):
        return list(self.members.values_list('code', flat=True))


class StockGroupMember(models.Model):
    group = models.ForeignKey(StockGroup, on_delete=models.CASCADE, related_name='members')
    code = models.CharField(max_length=10, db_index=True)

    class Meta:
        unique_together = ('group', 'code')


class ReportArtifact(models.Model):
    """报告产物登记（生成时间入库；HTML/JSON 文件本身在 artifacts/reports|json_data）。

    generated_at 初值取自产物 mtime（文件落盘时间即构建完成时间），首次查询时登记；
    重建后 mtime 变化会自动更新记录。报告页据此判定缓存是否过期。
    """
    code = models.CharField(max_length=10, primary_key=True)
    name = models.CharField(max_length=40, blank=True, default='')
    model = models.CharField(max_length=20, blank=True, default='')
    file_name = models.CharField(max_length=140, blank=True, default='')
    size_kb = models.IntegerField(default=0)
    data_last_date = models.CharField(max_length=10, blank=True, default='')
    generated_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['code']
