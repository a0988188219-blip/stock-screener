# -*- coding: utf-8 -*-
"""正式執行入口：取消股價上下限，只保留其他選股條件。"""
import screener

screener.MIN_PRICE = float("-inf")
screener.MAX_PRICE = float("inf")

if __name__ == "__main__":
    screener.main()
