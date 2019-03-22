# %%
import sqlite3
import pandas as pd


class Fund_DB:
    def __init__(self, database):
        self.conn = sqlite3.connect(database)  # 创建sqlite.db数据库
        print("open database success")
        self.cursor = self.conn.cursor()

    def connect_code(self, code):

        query = '''create table IF NOT EXISTS fd_%s (
            净值日期 TEXT,
            单位净值 NUMERIC,
            累计净值 NUMERIC,
            日增长率 NUMERIC
            )''' % (code)
        self.cursor.execute(query)
        print("Table connect successfully")
        self.conn.commit()

    def delete(self, code):
        self.cursor.execute("drop table IF EXISTS fd_%s" % (code))
        print("Table delete successfully")
        self.conn.commit()

    def insert_many(self, code, data):
        statement = "INSERT INTO fd_%s VALUES (?,?,?,?)" % (code)
        self.cursor.executemany(statement, data)
        self.conn.commit()

    def insert_one(self, code, data):
        print(data)
        self.cursor.execute("INSERT INTO fd_%s (净值日期,单位净值,累计净值,日增长率) \
            VALUES %s" % (code, data))
        self.conn.commit()

    def select(self, code, start=0, num=-1):
        cursor = self.cursor.execute("select * from fd_%s order by 净值日期 desc \
            limit %d offset %d" % (code, num, start))
        rows = cursor.fetchall()
        return rows

    def DataFrame(self, code, start=0, num=-1):
        df = pd.read_sql_query("select * from fd_%s order by 净值日期 asc \
            limit %d offset %d" % (code, num, start), self.conn)
        df.rename(columns={"净值日期": "d", "单位净值": "v1",
                           "累计净值": "v2", "日增长率": "r"}, inplace=True)
        
        # df.set_index('d', inplace=True)
        return df

    def show(self):
        self.cursor.execute("select name from sqlite_master where type='table' order by name")
        print(len(self.cursor.fetchall()))

        self.conn.commit()

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    from datetime import datetime
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fdb = Fund_DB('funds.db')
 
    rows = fdb.select('502023',num=360)

    d,v1,v2,r = zip(*rows)

    xs = [datetime.strptime(dd,'%Y-%m-%d').date() for dd in d]
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    plt.plot(xs, v1)
    plt.plot(xs, v2)

    plt.show()
    fdb.close()
