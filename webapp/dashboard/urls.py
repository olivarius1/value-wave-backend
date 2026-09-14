from django.urls import path

from . import views

urlpatterns = [
    path('', views.scores_page, name='home'),
    path('data/', views.data_page, name='data'),
    path('scores/', views.scores_page, name='scores'),
    path('report/', views.report_page, name='report'),
    path('groups/', views.groups_page, name='groups'),

    path('api/status/', views.api_status),
    path('api/search/', views.api_search),
    path('api/scores/', views.api_scores),
    path('api/report/status/', views.api_report_status),
    path('report/frame/<str:code>/', views.report_frame),

    path('api/boards/', views.api_boards),
    path('api/board/codes/', views.api_board_codes),
    path('api/groups/', views.api_groups),
    path('api/groups/create/', views.api_group_create),
    path('api/groups/set-stock/', views.api_group_set_stock),
    path('api/groups/<int:group_id>/delete/', views.api_group_delete),
    path('api/groups/<int:group_id>/', views.api_group_detail),

    path('api/jobs/', views.api_job_list),
    path('api/jobs/create/', views.api_job_create),
    path('api/jobs/<int:job_id>/cancel/', views.api_job_cancel),
]
