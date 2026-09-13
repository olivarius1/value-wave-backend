#!/usr/bin/env python3
"""Django 管理入口 —— A股估值系统 Web 控制台"""
import os
import sys


def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'webapp.settings')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
