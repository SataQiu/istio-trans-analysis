# -*- coding: UTF-8 -*-

import time
import datetime
import yaml
import os
import sqlite3
import requests
import re
from jinja2 import Template
from pyecharts import options as opts
from pyecharts.charts import Pie


WORKSPACE = "/trans_analysis"
CONFIG_FILE = os.path.join(WORKSPACE, 'config', 'config.yaml')


class Configuration(object):
    def __init__(self, configfile):
        with open(configfile, "r") as f:
            self.configure = yaml.safe_load(f)

    def get_config(self):
        return self.configure


class TransAnalysis(object):
    def __init__(self):
        self.config = Configuration(CONFIG_FILE).get_config()
        self.db = os.path.join(WORKSPACE, 'data', 'db.sqlite')
        self.github_token = self.config['github_token']

    def get_cursor(self):
        conn = sqlite3.connect(self.db)
        cursor = conn.cursor()
        return conn, cursor

    def ensure_tables(self):
        conn, cursor = self.get_cursor()

        cursor.execute(
            "SELECT * FROM sqlite_master WHERE type='table' AND name='pull_request'"
        )
        if cursor.fetchone() is None:
            cursor.execute('''
                            create table pull_request (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                number int(20),
                                github_id varchar(255),
                                merged_time varchar(100),
                                zh_word_count int(20),
                                base_branch varchar(100)
                            )''')
            print("create table pull_request successful")

        cursor.close()
        conn.commit()
        conn.close()

    def query_github_v4(self, query):
        r = requests.post("https://api.github.com/graphql",
                          json={"query": query},
                          headers={
                              "Authorization": "token %s" % self.github_token,
                              "Accept": "application/vnd.github.ocelot-preview+json",
                              "Accept-Encoding": "gzip"
                          })
        r.raise_for_status()
        reply = r.json()
        return reply

    def query_github_pr_diff(self, number):
        pr_diff_url = "https://github.com/" + self.config['repository']['owner'] + "/" + self.config['repository']['name'] + "/pull/" + str(number) + ".diff"
        r = requests.get(pr_diff_url)
        r.raise_for_status()
        return r.text

    def calc_zh_word_count(self, content):
        count = 0
        result = re.findall(u"[\u4e00-\u9fa5]", content)
        for cn in result:
            count += len(cn)
        return count

    def analysis_prs(self, next_cursor=""):
        batch_prs = []
        if next_cursor == "":
            query = Template("""
                query {
                    repository(name: "{{ name }}", owner: "{{ owner }}") {
                        pullRequests(
                            first: 100,
                            states: MERGED,
                            labels: "{{ trans_label }}") {
                            pageInfo {
                                endCursor
                                hasPreviousPage
                                hasNextPage
                            }
                            edges {
                                node {
                                    number
                                    author {
                                        login
                                    }
                                    baseRef {
                                        name
                                    }
                                    mergedAt
                                }
                            }
                        }
                    }
                }
                """).render({
                "name": self.config["repository"]["name"],
                "owner": self.config["repository"]["owner"],
                "trans_label": self.config["repository"]["trans_label"]
            })
            result = self.query_github_v4(query)
            has_next_page = result["data"]["repository"]["pullRequests"]["pageInfo"]["hasNextPage"]
            next_cursor = result["data"]["repository"]["pullRequests"]["pageInfo"]["endCursor"]
            prs = result["data"]["repository"]["pullRequests"]["edges"]
            for pr in prs:
                pr_number = pr["node"]["number"]
                pr_github_id = pr["node"]["author"]["login"]
                pr_merged_time = pr["node"]["mergedAt"]
                pr_base_branch = pr["node"]["baseRef"]["name"]
                batch_prs.append([pr_number, pr_github_id, pr_merged_time, pr_base_branch])
            self.insert_merged_prs(batch_prs)
            if has_next_page:
                time.sleep(1)
                self.analysis_prs(next_cursor)
        else:
            query = Template("""
                query {
                    repository(name: "{{ name }}", owner: "{{ owner }}") {
                        pullRequests(
                            first: 100,
                            states: MERGED,
                            labels: "{{ trans_label }}",
                            after: "{{ next_cursor }}" ) {
                            pageInfo {
                                endCursor
                                hasPreviousPage
                                hasNextPage
                            }
                            edges {
                                node {
                                    number
                                    author {
                                        login
                                    }
                                    baseRef {
                                        name
                                    }
                                    mergedAt
                                }
                            }
                        }
                    }
                }
                """).render({
                "name": self.config["repository"]["name"],
                "owner": self.config["repository"]["owner"],
                "trans_label": self.config["repository"]["trans_label"],
                "next_cursor": next_cursor
            })
            result = self.query_github_v4(query)
            has_next_page = result["data"]["repository"]["pullRequests"]["pageInfo"]["hasNextPage"]
            next_cursor = result["data"]["repository"]["pullRequests"]["pageInfo"]["endCursor"]
            prs = result["data"]["repository"]["pullRequests"]["edges"]
            for pr in prs:
                pr_number = pr["node"]["number"]
                pr_github_id = pr["node"]["author"]["login"]
                pr_merged_time = pr["node"]["mergedAt"]
                pr_base_branch = pr["node"]["baseRef"]["name"]
                batch_prs.append([pr_number, pr_github_id, pr_merged_time, pr_base_branch])
            self.insert_merged_prs(batch_prs)
            if has_next_page:
                time.sleep(1)
                self.analysis_prs(next_cursor)

    def insert_merged_prs(self, prs):
        conn, cursor = self.get_cursor()
        for pr in prs:
            number, github_id, merged_time, base_branch = pr[0], pr[1], pr[2], pr[3]
            cursor.execute("select * from pull_request where number = '" + str(number) + "'")
            if cursor.fetchone() is None:
                zh_word_count = self.calc_zh_word_count(self.query_github_pr_diff(number))
                print("analysis: pr_number ", number, "; zh_word_count ", zh_word_count)
                cursor.execute(
                    "insert into pull_request (number,github_id,merged_time,zh_word_count,base_branch) values ('"
                    + str(number) + "', '" + github_id + "','" + merged_time + "','" + str(zh_word_count) + "','" + base_branch + "')"
                )
                conn.commit()
                time.sleep(2)
        cursor.close()
        conn.commit()
        conn.close()


class ChartGenerator(object):
    def __init__(self, flag=1):
        self.config = Configuration(CONFIG_FILE).get_config()
        self.db = os.path.join(WORKSPACE, 'data', 'db.sqlite')
        self.start_time = self.config["duration"]["start"]
        self.end_time = self.config["duration"]["end"]
        if self.start_time == "":
            self.start_time = datetime.datetime.utcfromtimestamp(time.time()).strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.end_time == "":
            self.end_time = datetime.datetime.utcfromtimestamp(time.time()).strftime("%Y-%m-%dT%H:%M:%SZ")

    def gen_chart(self):
        select_sql = "select github_id,sum(zh_word_count) as total from pull_request " + \
            "where merged_time between '" + self.start_time + "' and  '" + self.end_time + "' " + \
            "and base_branch = '" + self.config["repository"]["branch"] + "' " + \
            "and number not in (" + self.config["except"] + ") " + \
            "group by github_id order by total desc limit 25"
        conn = sqlite3.connect(self.db)
        cursor = conn.cursor()
        cursor.execute(select_sql)
        data = cursor.fetchall()
        all_data = []
        showed_total = 0
        for zh in data:
            all_data.append([zh[0], zh[1]])
            showed_total += zh[1]

        select_sql = "select sum(zh_word_count) as total from pull_request " + \
            "where merged_time between '" + self.start_time + "' and  '" + self.end_time + "' " + \
            "and base_branch = '" + self.config["repository"]["branch"] + "' " + \
            "and number not in (" + self.config["except"] + ") "
        cursor.execute(select_sql)
        data = cursor.fetchone()
        total = data[0]
        other_count = total - showed_total
        all_data.append(["other", other_count])

        now_time = str(time.strftime('%Y-%m-%d', time.localtime(time.time()))).replace("-", "")

        # Pie chart
        pie = Pie(init_opts=opts.InitOpts(width="1200px", page_title=self.config["chart"]["title"]))
        pie.add(self.config["chart"]["series"], data_pair=all_data, center=["50%", "50%"]).set_global_opts(
            title_opts=opts.TitleOpts(title=self.config["chart"]["title"]),
            legend_opts=opts.LegendOpts(pos_top="10%", pos_left="80%", orient='vertical')
        )
        pie.render(WORKSPACE + "/output/" + now_time + "_page_pie.html")

        cursor.close()
        conn.commit()
        conn.close()


if __name__ == '__main__':
    trans_analysis = TransAnalysis()
    trans_analysis.ensure_tables()
    trans_analysis.analysis_prs()

    chart_gen = ChartGenerator()
    chart_gen.gen_chart()
    print("All work done ^_^")
